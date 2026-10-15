"""统一的 Portfolio 账户：所有品种共用一个现金账户（ADR-0001）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from quant.strategy import Side


@dataclass(frozen=True)
class Fill:
    date: date
    symbol: str
    side: Side
    quantity: int
    price: float
    fee: float

    @property
    def amount(self) -> float:
        return self.quantity * self.price


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, int] = field(default_factory=dict)

    def apply(self, fill: Fill) -> None:
        sign = 1 if fill.side is Side.BUY else -1
        self.cash -= sign * fill.amount + fill.fee
        quantity = self.positions.get(fill.symbol, 0) + sign * fill.quantity
        if quantity:
            self.positions[fill.symbol] = quantity
        else:
            self.positions.pop(fill.symbol, None)

    def accrue_interest(self, annual_rate: float, days: int) -> None:
        self.cash *= 1 + annual_rate * days / 365
