from datetime import date

import pytest

from quant.engine import BacktestConfig, run_backtest
from quant.instruments.stock import StockConfig
from quant.strategy import Order, Side, Strategy

from .conftest import make_feed

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
    assert list(result.daily["nav"]) == pytest.approx([1_000_000, 1_000_123.2877, 1_000_164.3886])


class BuyOnceOnFirstDay(Strategy):
    def __init__(self, quantity: int | None):
        self.quantity = quantity

    def on_bar(self, ctx):
        if len(ctx.history) == 1:
            return [Order(SYMBOL, Side.BUY, self.quantity)]
        return []


NO_COSTS = BacktestConfig(initial_cash=100_000, cash_rate=0.0, stock=StockConfig(commission_rate=0.0, min_commission=0.0))


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
                                 stock=StockConfig(commission_rate=0.00025, min_commission=5.0))
TWO_DAYS = {date(2024, 1, 2): (10, 10), date(2024, 1, 3): (10, 10)}


def test_full_position_buy_spends_as_much_cash_as_commission_allows():
    # 9997 股 × 10 = 99,970，佣金 99,970 × 万 2.5 = 24.9925，合计 99,994.99；再多 1 股就超出现金
    result = run_backtest(make_feed(SYMBOL, TWO_DAYS), BuyOnceOnFirstDay(None), symbol=SYMBOL,
                          start=date(2024, 1, 2), end=date(2024, 1, 3), config=WITH_COMMISSION)
    fill = result.fills.iloc[0]
    assert fill["quantity"] == 9997
    assert fill["fee"] == pytest.approx(24.9925)
    assert result.daily["cash"].iloc[-1] == pytest.approx(5.0075)


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
    assert (sell["side"], sell["quantity"], sell["price"], sell["fee"]) == ("sell", 1000, 12.0, 5.0)
    assert result.daily["positions"].iloc[-1] == {}
    # 100,000 − 10,000 − 5 + 12,000 − 5
    assert result.daily["nav"].iloc[-1] == pytest.approx(101_990)


def test_interest_for_the_gap_accrues_on_previous_close_cash_before_open_fills():
    # 周五收盘发出买入信号，周一开盘以 10 元买 1000 股：周末 3 天的利息按周五收盘的 100,000 现金计
    feed = make_feed(SYMBOL, {date(2024, 1, 5): (10, 10), date(2024, 1, 8): (10, 10)})
    config = BacktestConfig(initial_cash=100_000, cash_rate=0.015,
                            stock=StockConfig(commission_rate=0.0, min_commission=0.0))
    result = run_backtest(feed, BuyOnceOnFirstDay(1000), symbol=SYMBOL,
                          start=date(2024, 1, 5), end=date(2024, 1, 8), config=config)
    # 100,000 × (1 + 1.5% × 3 / 365) − 10,000
    assert result.daily["cash"].iloc[-1] == pytest.approx(90_012.3288)
