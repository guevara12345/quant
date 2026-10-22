from datetime import date

import pytest

from quant.engine import BacktestConfig, run_backtest
from quant.strategy import Order, Side, Strategy

from .conftest import make_bar, make_feed, stock_costs

SYMBOL = "sh.600000"


class DoNothing(Strategy):
    def on_bar(self, ctx):
        return []


def test_backtest_advances_only_on_trading_days():
    # 2024-01-03（周三）不在交易日历中
    feed = make_feed(
        SYMBOL,
        {
            date(2024, 1, 2): (10, 10),
            date(2024, 1, 4): (10, 10),
            date(2024, 1, 5): (10, 10),
        },
    )
    result = run_backtest(
        feed, DoNothing(), symbol=SYMBOL, start=date(2024, 1, 1), end=date(2024, 1, 4),
        config=BacktestConfig(initial_cash=100_000),
    )
    assert list(result.daily["date"]) == [date(2024, 1, 2), date(2024, 1, 4)]


def test_idle_cash_earns_annual_rate_per_calendar_day():
    # 周五 → 周一跨 3 个自然日，周一 → 周二 1 个自然日；每段按 1.5% / 365 × 天数计息并复利
    feed = make_feed(
        SYMBOL,
        {
            date(2024, 1, 5): (10, 10),
            date(2024, 1, 8): (10, 10),
            date(2024, 1, 9): (10, 10),
        },
    )
    result = run_backtest(
        feed, DoNothing(), symbol=SYMBOL, start=date(2024, 1, 5), end=date(2024, 1, 9),
        config=BacktestConfig(initial_cash=1_000_000, cash_rate=0.015),
    )
    assert list(result.daily["nav"]) == pytest.approx(
        [1_000_000, 1_000_123.2877, 1_000_164.3886])


class BuyOnceOnFirstDay(Strategy):
    def __init__(self, quantity: int | None):
        self.quantity = quantity

    def on_bar(self, ctx):
        if len(ctx.history) == 1:
            return [Order(SYMBOL, Side.BUY, self.quantity)]
        return []


NO_COSTS = BacktestConfig(initial_cash=100_000,
                          cash_rate=0.0, stock=stock_costs())


def test_stock_order_signalled_at_close_fills_at_next_day_open():
    feed = make_feed(
        SYMBOL,
        {
            date(2024, 1, 2): (10, 11),  # 信号日：收盘 11
            date(2024, 1, 3): (12, 13),  # 成交日：开盘 12
        },
    )
    result = run_backtest(feed, BuyOnceOnFirstDay(1000), symbol=SYMBOL,
                          start=date(2024, 1, 2), end=date(2024, 1, 3), config=NO_COSTS)

    fills = result.fills.to_dict("records")
    assert fills == [{"date": date(2024, 1, 3), "symbol": SYMBOL, "side": "buy",
                      "quantity": 1000, "price": 12.0, "fee": 0.0}]
    assert [p.get(SYMBOL, 0) for p in result.daily["positions"]] == [0, 1000]
    assert list(result.daily["cash"]) == [100_000, 88_000]
    assert list(result.daily["nav"]) == [100_000, 88_000 + 1000 * 13]


WITH_COMMISSION = BacktestConfig(initial_cash=100_000, cash_rate=0.0,
                                 stock=stock_costs(commission_rate=0.00025, min_commission=5.0))
TWO_DAYS = {date(2024, 1, 2): (10, 10), date(2024, 1, 3): (10, 10)}


def test_full_position_buy_takes_as_many_whole_lots_as_cash_and_commission_allow():
    # 10,000 股需要 100,000 + 佣金 25，超出现金；退到 9,900 股 × 10 = 99,000，佣金 24.75
    result = run_backtest(make_feed(SYMBOL, TWO_DAYS), BuyOnceOnFirstDay(None), symbol=SYMBOL,
                          start=date(2024, 1, 2), end=date(2024, 1, 3), config=WITH_COMMISSION)
    fill = result.fills.iloc[0]
    assert fill["quantity"] == 9900
    assert fill["fee"] == pytest.approx(24.75)
    assert result.daily["cash"].iloc[-1] == pytest.approx(975.25)


def test_small_trade_pays_minimum_commission():
    result = run_backtest(make_feed(SYMBOL, TWO_DAYS), BuyOnceOnFirstDay(100), symbol=SYMBOL,
                          start=date(2024, 1, 2), end=date(2024, 1, 3), config=WITH_COMMISSION)
    assert result.fills.iloc[0]["fee"] == 5.0
    assert result.daily["cash"].iloc[-1] == 100_000 - 1000 - 5


class BuyThenSellAll(Strategy):
    def on_bar(self, ctx):
        if len(ctx.history) == 1:
            return [Order(SYMBOL, Side.BUY, 1000)]
        if len(ctx.history) == 2:
            return [Order(SYMBOL, Side.SELL)]
        return []


def test_sell_without_quantity_closes_whole_position_at_next_open():
    feed = make_feed(SYMBOL, {
        date(2024, 1, 2): (10, 10),
        date(2024, 1, 3): (10, 11),  # 买入成交；收盘发出卖出信号
        date(2024, 1, 4): (12, 12),  # 卖出成交于开盘 12
    })
    result = run_backtest(feed, BuyThenSellAll(), symbol=SYMBOL,
                          start=date(2024, 1, 2), end=date(2024, 1, 4), config=WITH_COMMISSION)
    sell = result.fills.iloc[-1]
    assert (sell["side"], sell["quantity"], sell["price"],
            sell["fee"]) == ("sell", 1000, 12.0, 5.0)
    assert result.daily["positions"].iloc[-1] == {}
    # 100,000 − 10,000 − 5 + 12,000 − 5
    assert result.daily["nav"].iloc[-1] == pytest.approx(101_990)


def test_interest_for_the_gap_accrues_on_previous_close_cash_before_open_fills():
    # 周五收盘发出买入信号，周一开盘以 10 元买 1000 股：周末 3 天的利息按周五收盘的 100,000 现金计
    feed = make_feed(SYMBOL, {date(2024, 1, 5): (
        10, 10), date(2024, 1, 8): (10, 10)})
    config = BacktestConfig(initial_cash=100_000, cash_rate=0.015,
                            stock=stock_costs())
    result = run_backtest(feed, BuyOnceOnFirstDay(1000), symbol=SYMBOL,
                          start=date(2024, 1, 5), end=date(2024, 1, 8), config=config)
    # 100,000 × (1 + 1.5% × 3 / 365) − 10,000
    assert result.daily["cash"].iloc[-1] == pytest.approx(90_012.3288)


def test_position_scales_by_factor_ratio_on_ex_rights_day_so_nav_does_not_drop():
    # 01-03 是除权日：每 10 股送 1 股，因子 1.0 → 1.1，不复权价 11 → 10，后复权价连续
    # 1000 股 × 11 = 11,000；除权后 1000 × 1.1 = 1100 股 × 10 = 11,000，净值不变
    feed = make_feed(SYMBOL, {
        date(2024, 1, 2): (10, 10),
        date(2024, 1, 3): (10.5, 11),  # 开盘 10.5 买入成交
        date(2024, 1, 4): make_bar(10, 10, adj_factor=1.1),
    })
    result = run_backtest(feed, BuyOnceOnFirstDay(1000), symbol=SYMBOL,
                          start=date(2024, 1, 2), end=date(2024, 1, 4), config=NO_COSTS)
    assert [p.get(SYMBOL, 0) for p in result.daily["positions"]] == [0, 1000, 1100]
    # 100,000 − 10,500 + 11,000
    assert list(result.daily["nav"]) == pytest.approx([100_000, 100_500, 100_500])


def test_ex_rights_adjusted_position_rounds_to_whole_shares_and_can_all_be_sold():
    # 现金分红再投资：因子比例 1.0123，1000 股 → 1012.3 股，取最近的整股 1012（零股可以卖出）
    feed = make_feed(SYMBOL, {
        date(2024, 1, 2): (10, 10),
        date(2024, 1, 3): (10, 10),  # 买入成交；收盘发出卖出信号
        date(2024, 1, 4): make_bar(9.9, 9.9, adj_factor=1.0123),  # 除权日，卖出成交
    })
    result = run_backtest(feed, BuyThenSellAll(), symbol=SYMBOL,
                          start=date(2024, 1, 2), end=date(2024, 1, 4), config=NO_COSTS)
    assert result.fills.iloc[-1]["quantity"] == 1012
    assert result.daily["positions"].iloc[-1] == {}
