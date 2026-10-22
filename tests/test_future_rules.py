"""期货品种规则（ticket 04）：当日收盘价成交、按结算价盯市、保证金占用、平今平昨费率、到期强制平仓。"""

from datetime import date

import pytest

from quant.engine import BacktestConfig, run_backtest
from quant.strategy import Order, Side, Strategy

from .conftest import future_costs, make_future_bar, make_multi_feed

IC = "IC2403"  # 2024-03-15（第三个周五）到期
D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)

NO_COSTS = BacktestConfig(initial_cash=3_000_000,
                          cash_rate=0.0, future=future_costs())


class Script(Strategy):
    """按日期发出预先写好的订单。"""

    def __init__(self, orders: dict[date, list[Order]], universe: tuple[str, ...] = (IC,)):
        self.orders = orders
        self.symbols = list(universe)

    def universe(self, day):
        return self.symbols

    def on_bar(self, ctx):
        return self.orders.get(ctx.day, [])


def test_futures_fill_at_close_and_mark_to_market_at_settle_into_cash():
    # 开仓 1 手 @ 收盘 5000：名义本金 5000 × 200 = 1,000,000，现金不动
    # 第 1 天结算价 5010：+10 × 200；第 2 天 4990：−20 × 200；第 3 天平仓 @ 4995：+5 × 200
    feed = make_multi_feed({IC: {
        D1: make_future_bar(5000, settle=5010),
        D2: make_future_bar(4985, settle=4990),
        D3: make_future_bar(4995, settle=5000),
    }})
    strategy = Script({D1: [Order(IC, Side.BUY, 1)],
                      D3: [Order(IC, Side.SELL)]})
    result = run_backtest(feed, strategy, start=D1,
                          end=D3, config=NO_COSTS)

    assert result.fills[["date", "side", "quantity", "price", "multiplier"]].to_dict("records") == [
        {"date": D1, "side": "buy", "quantity": 1,
            "price": 5000.0, "multiplier": 200},
        {"date": D3, "side": "sell", "quantity": 1,
            "price": 4995.0, "multiplier": 200},
    ]
    assert list(result.daily["cash"]) == pytest.approx(
        [3_002_000, 2_998_000, 2_999_000])
    assert list(result.daily["nav"]) == list(result.daily["cash"])
    assert result.daily["positions"].iloc[-1] == {}


def test_margin_occupied_is_notional_at_settle_times_14_percent_and_does_not_reduce_cash():
    feed = make_multi_feed({IC: {
        D1: make_future_bar(5000, settle=5000),
        D2: make_future_bar(5100, settle=5050),
        D3: make_future_bar(5100, settle=5100),
    }})
    strategy = Script({D1: [Order(IC, Side.BUY, 2)],
                      D3: [Order(IC, Side.SELL)]})
    result = run_backtest(feed, strategy, start=D1,
                          end=D3, config=NO_COSTS)

    # 2 手 × 结算价 × 200 × 14%；平仓后归零
    assert list(result.daily["margin"]) == pytest.approx(
        [2 * 5000 * 200 * 0.14, 2 * 5050 * 200 * 0.14, 0])
    assert result.daily["cash"].iloc[0] == 3_000_000


def test_closing_today_position_pays_ten_times_the_close_yesterday_fee():
    # 价格恒为 5000，每手名义本金 1,000,000。第 1 天开 3 手；第 2 天再开 1 手后平 4 手：
    # 先平昨 3 手（万 0.23），再平今 1 手（万 2.3）
    flat = make_future_bar(5000)
    feed = make_multi_feed({IC: {D1: flat, D2: flat}})
    config = BacktestConfig(initial_cash=3_000_000, cash_rate=0.0, future=future_costs(
        open_fee_rate=0.000023, close_yesterday_fee_rate=0.000023, close_today_fee_rate=0.00023))
    strategy = Script({D1: [Order(IC, Side.BUY, 3)],
                       D2: [Order(IC, Side.BUY, 1), Order(IC, Side.SELL, 4)]})
    result = run_backtest(feed, strategy, start=D1, end=D2, config=config)

    assert list(result.fills["fee"]) == pytest.approx(
        [3 * 23, 23, 3 * 23 + 230])
    assert result.daily["cash"].iloc[-1] == pytest.approx(
        3_000_000 - 69 - 23 - 299)


def test_position_still_held_on_expiry_day_is_force_closed_at_settle_with_alert():
    # IC2403 的到期日是 2024-03 的第三个周五 03-15；策略没有展期
    days = [date(2024, 3, 14), date(2024, 3, 15), date(2024, 3, 18)]
    feed = make_multi_feed({IC: {
        days[0]: make_future_bar(5000),
        days[1]: make_future_bar(5100, settle=5050),
    }}, calendar=days)
    result = run_backtest(feed, Script({days[0]: [Order(IC, Side.BUY, 1)]}),
                          start=days[0], end=days[2], config=NO_COSTS)

    assert result.fills[["date", "side", "quantity", "price"]].to_dict("records")[-1] == {
        "date": days[1], "side": "sell", "quantity": 1, "price": 5050.0}
    assert list(result.alerts["date"]) == [days[1]]
    assert "到期" in result.alerts["message"].iloc[0]
    assert [p.get(IC, 0) for p in result.daily["positions"]] == [1, 0, 0]
    assert result.daily["cash"].iloc[-1] == pytest.approx(
        3_000_000 + 50 * 200)


def test_expiry_on_a_holiday_third_friday_is_postponed_to_next_trading_day():
    # 2018-02 的第三个周五 02-16 是春节，IC1802 顺延到节后第一个交易日 02-22 到期
    contract = "IC1802"
    days = [date(2018, 2, 14), date(2018, 2, 22), date(2018, 2, 23)]
    feed = make_multi_feed(
        {contract: {d: make_future_bar(6000) for d in days}}, calendar=days)
    strategy = Script({days[0]: [Order(contract, Side.BUY, 1)]}, universe=(contract,))
    result = run_backtest(feed, strategy, start=days[0],
                          end=days[2], config=NO_COSTS)

    assert list(result.alerts["date"]) == [days[1]]
    assert [p.get(contract, 0)
            for p in result.daily["positions"]] == [1, 0, 0]


def test_opening_is_rejected_when_cash_cannot_cover_margin_and_fee():
    # 每手保证金 5000 × 200 × 14% = 140,000；现金 200,000 只够开 1 手
    feed = make_multi_feed({IC: {D1: make_future_bar(5000)}})
    config = BacktestConfig(initial_cash=200_000,
                            cash_rate=0.0, future=future_costs())
    result = run_backtest(feed, Script({D1: [Order(IC, Side.BUY, 1), Order(IC, Side.BUY, 1)]}),
                          start=D1, end=D1, config=config)

    assert list(result.fills["quantity"]) == [1]
    assert list(result.rejections["reason"]) == ["资金不足"]


def test_cash_occupied_as_margin_earns_no_interest():
    # 周五开 1 手 @ 5000，保证金 140,000；周末 3 天只有 3,000,000 − 140,000 的闲置现金按 1.5% 计息
    fri, mon = date(2024, 1, 5), date(2024, 1, 8)
    feed = make_multi_feed({IC: {fri: make_future_bar(5000), mon: make_future_bar(5000)}})
    config = BacktestConfig(initial_cash=3_000_000,
                            cash_rate=0.015, future=future_costs())
    result = run_backtest(feed, Script({fri: [Order(IC, Side.BUY, 1)]}),
                          start=fri, end=mon, config=config)

    assert result.daily["cash"].iloc[-1] == pytest.approx(
        3_000_000 + 2_860_000 * 0.015 * 3 / 365)
