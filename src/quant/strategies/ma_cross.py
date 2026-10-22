"""双均线策略：快线上穿慢线满仓买入，下穿全部卖出。均线基于后复权价计算。"""

from __future__ import annotations

from datetime import date

from quant.strategy import Context, Order, Side, Strategy


class MaCross(Strategy):
    def __init__(self, symbol: str, fast: int = 5, slow: int = 20):
        if not 0 < fast < slow:
            raise ValueError(f"需要 0 < fast < slow，实际 fast={fast}, slow={slow}")
        self.symbol = symbol
        self.fast = fast
        self.slow = slow

    def universe(self, day: date) -> list[str]:
        return [self.symbol]

    def on_bar(self, ctx: Context) -> list[Order]:
        prices = [bar.adj_close for _, bar in ctx.history(
            self.symbol)[-(self.slow + 1):]]
        if len(prices) < self.slow + 1:
            return []
        today = _mean(prices[-self.fast:]) - _mean(prices[-self.slow:])
        yesterday = _mean(prices[-self.fast - 1:-1]) - \
            _mean(prices[-self.slow - 1:-1])
        held = ctx.positions.get(self.symbol, 0)
        if yesterday <= 0 < today and held == 0:
            return [Order(self.symbol, Side.BUY)]
        if yesterday >= 0 > today and held > 0:
            return [Order(self.symbol, Side.SELL)]
        return []


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)
