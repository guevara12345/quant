from datetime import date

import pytest

from quant.engine import BacktestConfig, run_backtest
from quant.metrics import compute_metrics
from quant.strategy import Order, Side, Strategy

from .conftest import future_costs, make_feed, make_future_bar, make_multi_feed, stock_costs

SYMBOL = "sh.600519"


class BuyDay1SellDay4(Strategy):
    def universe(self, day):
        return [SYMBOL]

    def on_bar(self, ctx):
        n = len(ctx.history(SYMBOL))
        if n == 1:
            return [Order(SYMBOL, Side.BUY)]
        if n == 4:
            return [Order(SYMBOL, Side.SELL)]
        return []


def test_metrics_and_buy_and_hold_benchmark():
    # 开盘 10 满仓买入、开盘 11 卖出；基准买入持有：收盘价 10 → 9，收益 −10%
    feed = make_feed(SYMBOL, {
        date(2024, 1, 2): (10, 10),
        date(2024, 1, 3): (10, 10),
        date(2024, 1, 4): (10, 8),
        date(2024, 1, 5): (10, 12),
        date(2024, 1, 8): (11, 9),
    })
    config = BacktestConfig(initial_cash=10_000, cash_rate=0.0,
                            stock=stock_costs(commission_rate=0.001))
    result = run_backtest(feed, BuyDay1SellDay4(), start=date(2024, 1, 2), end=date(2024, 1, 8), config=config)
    m = compute_metrics(result, feed, SYMBOL)

    # 只收千 1 佣金便于手算：满仓买 900 股（9,000 + 9 费用；1,000 股连佣金超出现金），卖出 900 × 11 = 9,900，费用 9.9
    assert result.fills["quantity"].tolist() == [900, 900]
    assert m["total_fees"] == pytest.approx(9 + 9.9)
    # 期末现金 10,000 − 9,000 − 9 + 9,900 − 9.9 = 10,881.1
    assert m["total_return"] == pytest.approx(0.08811)
    # 回撤从 10,000 到 1 月 4 日的 991 + 900 × 8 = 8,191
    assert m["max_drawdown"] == pytest.approx(1 - 8_191 / 10_000)
    assert m["win_rate"] == 1.0
    assert m["trade_count"] == 1
    assert m["benchmark_total_return"] == pytest.approx(-0.10)


class FutureRoundTrip(Strategy):
    def universe(self, day):
        return ["IC2403"]

    def on_bar(self, ctx):
        side = {1: Side.BUY, 2: Side.SELL}.get(len(ctx.history("IC2403")))
        return [Order("IC2403", side, 1)] if side else []


def test_futures_round_trip_is_judged_on_notional_including_multiplier():
    # 5000 买、5001 卖 1 手：盈利 1 × 200 = 200，开平各万 0.23 ≈ 23 → 净赚，算一次盈利交易
    feed = make_multi_feed({"IC2403": {
        date(2024, 1, 2): make_future_bar(5000),
        date(2024, 1, 3): make_future_bar(5001),
    }})
    config = BacktestConfig(initial_cash=3_000_000, cash_rate=0.0,
                            future=future_costs(open_fee_rate=0.000023, close_yesterday_fee_rate=0.000023))
    result = run_backtest(feed, FutureRoundTrip(), start=date(2024, 1, 2),
                          end=date(2024, 1, 3), config=config)
    m = compute_metrics(result, feed, "IC2403")

    assert (m["win_rate"], m["trade_count"]) == (1.0, 1)
    assert result.daily["nav"].iloc[-1] == pytest.approx(
        3_000_000 + 200 - 5000 * 200 * 0.000023 - 5001 * 200 * 0.000023)
