"""ETF 期权品种规则（ticket 05）：按模型价收盘成交与估值、每张 2 元费用、到期按内在价值结算。"""

from datetime import date, timedelta

import pytest

from quant.data.feed import InMemoryDataFeed
from quant.engine import BacktestConfig, run_backtest
from quant.strategy import Order, Side, Strategy

from .conftest import make_bar

ETF = "sh.510500"
PUT = "510500P2403M05000"  # 行权价 5.000，2024-03 第四个星期三 03-27 到期
BUY_DAY, EXPIRY = date(2024, 3, 1), date(2024, 3, 27)
NO_INTEREST = BacktestConfig(initial_cash=1_000_000, cash_rate=0.0)


def weekdays(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5]


def etf_feed(level: float, last: float | None = None, days: list[date] | None = None,
             overrides: dict[date, float] | None = None) -> InMemoryDataFeed:
    """510500 在 level 与 level × 1.01 之间逐日交替（已实现波动率恒定），last 给出最后一天的收盘价。"""
    days = days or weekdays(date(2024, 1, 2), date(2024, 3, 29))
    closes = {d: level * (1.01 if i % 2 else 1.0) for i, d in enumerate(days)}
    if last is not None:
        closes[days[-1]] = last
    closes.update(overrides or {})
    return InMemoryDataFeed(calendar=days, bars={ETF: {d: make_bar(c) for d, c in closes.items()}})


class Script(Strategy):
    def __init__(self, orders: dict[date, list[Order]], universe: tuple[str, ...] = (PUT,)):
        self.orders = orders
        self.symbols = list(universe)

    def universe(self, day):
        return self.symbols

    def on_bar(self, ctx):
        return self.orders.get(ctx.day, [])


def run_to(feed, end, orders=None, start=BUY_DAY):
    return run_backtest(feed, Script(orders or {start: [Order(PUT, Side.BUY, 1)]}),
                        start=start, end=end, config=NO_INTEREST)


def test_out_of_the_money_put_held_to_expiry_expires_worthless_costing_premium_plus_fee():
    result = run_to(etf_feed(5.5, days=weekdays(date(2024, 1, 2), EXPIRY), last=5.6), EXPIRY)

    buy, settle = result.fills.to_dict("records")
    premium = buy["price"]
    assert (buy["side"], buy["quantity"], buy["fee"], buy["multiplier"]) == ("buy", 1, 2.0, 10000)
    assert 0 < premium < 0.1
    assert (settle["date"], settle["side"], settle["price"], settle["fee"]) == (EXPIRY, "sell", 0.0, 0.0)
    assert result.daily["cash"].iloc[-1] == pytest.approx(1_000_000 - premium * 10000 - 2)
    assert result.daily["positions"].iloc[-1] == {}
    assert list(result.alerts["date"]) == [EXPIRY]


def test_in_the_money_put_held_to_expiry_settles_at_intrinsic_value_of_etf_close():
    # 到期日 ETF 收盘 4.6，行权价 5 → 内在价值 0.4 × 10000 = 4000
    result = run_to(etf_feed(5.5, days=weekdays(date(2024, 1, 2), EXPIRY), last=4.6), EXPIRY)

    premium = result.fills["price"].iloc[0]
    assert result.fills["price"].iloc[-1] == pytest.approx(0.4)
    assert result.daily["cash"].iloc[-1] == pytest.approx(1_000_000 - premium * 10000 - 2 + 4000)


def test_expiry_on_a_holiday_fourth_wednesday_is_postponed_to_next_trading_day():
    # 2023-01 的第四个星期三 01-25 在春节假期（01-21..01-27）内，510500P2301 顺延到 01-30 到期
    put = "510500P2301M06000"
    holiday = {date(2023, 1, d) for d in range(23, 28)}
    days = [d for d in weekdays(date(2022, 11, 1), date(2023, 2, 3)) if d not in holiday]
    feed = etf_feed(6.0, days=days)
    buy = date(2023, 1, 3)
    result = run_backtest(feed, Script({buy: [Order(put, Side.BUY, 1)]}, universe=(put,)),
                          start=buy, end=days[-1], config=NO_INTEREST)

    assert list(result.alerts["date"]) == [date(2023, 1, 30)]
    assert result.fills["date"].iloc[-1] == date(2023, 1, 30)


def test_held_put_is_valued_daily_at_model_price_that_decays_with_time_and_rises_as_etf_falls():
    # 买入平值附近的 put 后：同一 ETF 价位上，隔两天（交替序列同相位）净值因时间价值衰减而下降；
    # 最后一天 ETF 跌到 5.0，put 升值、净值上升
    days = weekdays(date(2024, 1, 2), date(2024, 3, 8))
    result = run_to(etf_feed(5.5, days=days, last=5.0), days[-1], start=days[-6])
    nav = list(result.daily["nav"])

    assert nav[3] < nav[1]
    assert nav[4] < nav[2]
    assert nav[-1] > max(nav[:-1])
    market_value = result.daily["market_value"]
    assert (market_value > 0).all()
    assert list(result.daily["cash"] + market_value) == pytest.approx(nav)


def test_buying_puts_beyond_available_cash_is_rejected():
    feed = etf_feed(5.5)
    config = BacktestConfig(initial_cash=10.0, cash_rate=0.0)
    result = run_backtest(feed, Script({BUY_DAY: [Order(PUT, Side.BUY, 1)]}),
                          start=BUY_DAY, end=BUY_DAY, config=config)

    assert result.fills.empty
    assert list(result.rejections["reason"]) == ["资金不足"]


def test_put_has_no_quote_until_etf_history_covers_the_volatility_window():
    # ETF 只有 10 个交易日的历史，不足 20 日波动率窗口 → 当日无报价。universe 带上 ETF，策略才会被调用
    days = weekdays(date(2024, 2, 19), date(2024, 3, 1))
    result = run_backtest(etf_feed(5.5, days=days), Script({BUY_DAY: [Order(PUT, Side.BUY, 1)]}, universe=(PUT, ETF)),
                          start=BUY_DAY, end=BUY_DAY, config=NO_INTEREST)

    assert result.fills.empty
    assert list(result.rejections["reason"]) == ["无行情"]


def test_expired_put_has_no_quote_so_it_cannot_be_bought_after_expiry():
    after = date(2024, 3, 28)
    result = run_backtest(etf_feed(5.5), Script({after: [Order(PUT, Side.BUY, 1)]}, universe=(PUT, ETF)),
                          start=after, end=after, config=NO_INTEREST)

    assert result.fills.empty
    assert list(result.rejections["reason"]) == ["无行情"]
