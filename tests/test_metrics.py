from datetime import date

import pytest

from quant.engine import BacktestConfig, run_backtest
from quant.instruments.stock import StockConfig
from quant.metrics import compute_metrics
from quant.strategy import Order, Side, Strategy

from .conftest import make_feed

SYMBOL = "sh.600519"


class BuyDay1SellDay4(Strategy):
    def on_bar(self, ctx):
        n = len(ctx.history)
        if n == 1:
            return [Order(SYMBOL, Side.BUY)]
        if n == 4:
            return [Order(SYMBOL, Side.SELL)]
        return []


def test_metrics_and_buy_and_hold_benchmark():
    # 净值：10,000 → 10,000（开盘 10 买 1000 股）→ 8,000 → 12,000 → 11,000（开盘 11 卖出）
    # 基准买入持有：收盘价 10 → 9，收益 −10%
    feed = make_feed(SYMBOL, {
        date(2024, 1, 2): (10, 10),
        date(2024, 1, 3): (10, 10),
        date(2024, 1, 4): (10, 8),
        date(2024, 1, 5): (10, 12),
        date(2024, 1, 8): (11, 9),
    })
    config = BacktestConfig(initial_cash=10_000, cash_rate=0.0,
                            stock=StockConfig(commission_rate=0.001, min_commission=0.0))
    result = run_backtest(feed, BuyDay1SellDay4(), symbol=SYMBOL,
                          start=date(2024, 1, 2), end=date(2024, 1, 8), config=config)
    m = compute_metrics(result, feed, SYMBOL)

    # 万 2.5 改成千 1 便于手算：满仓买 999 股（9,990 + 9.99 费用），卖出 999 × 11 = 10,989，费用 10.989
    assert result.fills["quantity"].tolist() == [999, 999]
    assert m["total_fees"] == pytest.approx(9.99 + 10.989)
    # 期末现金 10,000 − 9,990 − 9.99 + 10,989 − 10.989 = 10,978.021
    assert m["total_return"] == pytest.approx(0.0978021)
    # 回撤从 10,000 到 1 月 4 日的 0.01 + 999 × 8 = 7,992.01
    assert m["max_drawdown"] == pytest.approx(1 - 7_992.01 / 10_000)
    assert m["win_rate"] == 1.0
    assert m["trade_count"] == 1
    assert m["benchmark_total_return"] == pytest.approx(-0.10)
