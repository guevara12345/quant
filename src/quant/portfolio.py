"""统一的 Portfolio 账户：所有品种共用一个现金账户（ADR-0001）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

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


class RejectReason(str, Enum):
    LIMIT_UP = "涨停"
    LIMIT_DOWN = "跌停"
    SUSPENDED = "停牌"
    T_PLUS_1 = "T+1"
    INSUFFICIENT_CASH = "资金不足"
    ODD_LOT = "不足整手"
    NO_POSITION = "无持仓"


@dataclass(frozen=True)
class Rejection:
    """未成交的订单及原因；quantity 是订单上的数量（None 表示满仓买入 / 全部卖出）。"""

    date: date
    symbol: str
    side: Side
    quantity: int | None
    reason: RejectReason


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, int] = field(default_factory=dict)
    # T+1：最近一个有买入成交的日期，以及当日买入的股数
    _bought_day: date | None = field(default=None, repr=False)
    _bought_today: dict[str, int] = field(default_factory=dict, repr=False)

    def sellable(self, symbol: str, day: date) -> int:
        """可卖数量：持股数减去当日买入的部分。"""
        bought = self._bought_today.get(
            symbol, 0) if day == self._bought_day else 0
        return self.positions.get(symbol, 0) - bought

    def apply(self, fill: Fill) -> None:
        sign = 1 if fill.side is Side.BUY else -1
        if fill.side is Side.BUY:
            if fill.date != self._bought_day:
                self._bought_day, self._bought_today = fill.date, {}
            self._bought_today[fill.symbol] = self._bought_today.get(
                fill.symbol, 0) + fill.quantity
        self.cash -= sign * fill.amount + fill.fee
        quantity = self.positions.get(fill.symbol, 0) + sign * fill.quantity
        if quantity:
            self.positions[fill.symbol] = quantity
        else:
            self.positions.pop(fill.symbol, None)

    def accrue_interest(self, annual_rate: float, days: int) -> None:
        self.cash *= 1 + annual_rate * days / 365
