"""股票品种规则（基础版）：按成交日开盘价（不复权）成交，只收佣金。

T+1、整手、涨跌停、停牌、印花税、过户费、滑点等完整 A 股规则在后续 ticket 中补充。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from quant.data.feed import Bar
from quant.portfolio import Fill, Portfolio
from quant.strategy import Order, Side


@dataclass(frozen=True)
class StockConfig:
    commission_rate: float = 0.00025
    min_commission: float = 5.0


class StockRules:
    def __init__(self, config: StockConfig):
        self.config = config

    def commission(self, amount: float) -> float:
        if amount <= 0:
            return 0.0
        return max(amount * self.config.commission_rate, self.config.min_commission)

    def match_at_open(self, order: Order, day: date, bar: Bar, portfolio: Portfolio) -> Fill | None:
        price = bar.open
        if order.side is Side.BUY:
            quantity = order.quantity if order.quantity is not None else self._max_affordable(price, portfolio.cash)
        else:
            held = portfolio.positions.get(order.symbol, 0)
            quantity = held if order.quantity is None else min(order.quantity, held)
        if quantity <= 0:
            return None
        return Fill(day, order.symbol, order.side, quantity, price, self.commission(quantity * price))

    def _max_affordable(self, price: float, cash: float) -> int:
        """满仓买入：现金（含佣金）能买到的最大股数，先简单取整。"""
        quantity = math.floor(cash / (price * (1 + self.config.commission_rate)))
        while quantity > 0 and quantity * price + self.commission(quantity * price) > cash:
            quantity -= 1
        return quantity
