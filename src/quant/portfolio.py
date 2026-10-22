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
    multiplier: int = 1  # 合约乘数，股票为 1

    @property
    def amount(self) -> float:
        """成交金额；衍生品为名义本金。"""
        return self.quantity * self.price * self.multiplier


class RejectReason(str, Enum):
    LIMIT_UP = "涨停"
    LIMIT_DOWN = "跌停"
    SUSPENDED = "停牌"
    T_PLUS_1 = "T+1"
    INSUFFICIENT_CASH = "资金不足"
    ODD_LOT = "不足整手"
    NO_POSITION = "无持仓"
    NO_QUOTE = "无行情"


@dataclass(frozen=True)
class Rejection:
    """未成交的订单及原因；quantity 是订单上的数量（None 表示满仓买入 / 全部卖出）。"""

    date: date
    symbol: str
    side: Side
    quantity: int | None
    reason: RejectReason


@dataclass(frozen=True)
class Alert:
    """需要研究者注意、但不中断回测的事件，如持仓合约到期被强制平仓。"""

    date: date
    symbol: str
    message: str


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, int] = field(default_factory=dict)
    # 逐日盯市品种（期货）的盯市基准价：上一次结算价，或当日开仓的成交价
    marks: dict[str, float] = field(default_factory=dict)
    # 保证金占用，只记录不扣现金
    margins: dict[str, float] = field(default_factory=dict)
    # 最近一个有买入成交的日期，以及当日买入（开仓）的数量：股票 T+1、期货区分平今平昨都要用
    _bought_day: date | None = field(default=None, repr=False)
    _bought_today: dict[str, int] = field(default_factory=dict, repr=False)

    def sellable(self, symbol: str, day: date) -> int:
        """可卖数量：持仓减去当日买入的部分（期货即可平的昨仓）。"""
        return self.positions.get(symbol, 0) - self.bought_today(symbol, day)

    def bought_today(self, symbol: str, day: date) -> int:
        return self._bought_today.get(symbol, 0) if day == self._bought_day else 0

    def apply(self, fill: Fill) -> None:
        """现金交割的成交（股票）：买入付出成交金额，卖出收回成交金额。"""
        sign = 1 if fill.side is Side.BUY else -1
        self.cash -= sign * fill.amount + fill.fee
        self._update_position(fill)

    def apply_marked(self, fill: Fill) -> None:
        """逐日盯市的成交（期货）：不交割名义本金，只扣费用。

        开仓把成交价按数量加权并入盯市基准价；平仓实现 (成交价 − 基准价) × 数量 × 乘数 的盈亏。
        """
        held = self.positions.get(fill.symbol, 0)
        if fill.side is Side.BUY:
            mark = self.marks.get(fill.symbol, fill.price)
            self.marks[fill.symbol] = (
                held * mark + fill.quantity * fill.price) / (held + fill.quantity)
        else:
            self.cash += fill.quantity * fill.multiplier * (fill.price - self.marks[fill.symbol])
        self.cash -= fill.fee
        self._update_position(fill)
        if fill.symbol not in self.positions:
            self.marks.pop(fill.symbol, None)

    def mark_to_market(self, symbol: str, price: float, multiplier: int) -> None:
        """按结算价盯市：(结算价 − 基准价) × 持仓 × 乘数 计入现金，基准价更新为结算价。"""
        held = self.positions.get(symbol, 0)
        self.cash += held * multiplier * (price - self.marks[symbol])
        self.marks[symbol] = price

    def _update_position(self, fill: Fill) -> None:
        if fill.side is Side.BUY:
            if fill.date != self._bought_day:
                self._bought_day, self._bought_today = fill.date, {}
            self._bought_today[fill.symbol] = self._bought_today.get(
                fill.symbol, 0) + fill.quantity
            quantity = self.positions.get(fill.symbol, 0) + fill.quantity
        else:
            # 先平昨仓、后平今仓：超出昨仓的部分从当日买入中扣除（股票受 T+1 约束，不会走到这里）
            closed_today = fill.quantity - max(self.sellable(fill.symbol, fill.date), 0)
            if closed_today > 0:
                self._bought_today[fill.symbol] -= closed_today
            quantity = self.positions.get(fill.symbol, 0) - fill.quantity
        if quantity:
            self.positions[fill.symbol] = quantity
        else:
            self.positions.pop(fill.symbol, None)

    def adjust_position(self, symbol: str, quantity: int) -> None:
        """公司行为（如除权）直接改写持股数：不产生成交，不动现金。"""
        if quantity:
            self.positions[symbol] = quantity
        else:
            self.positions.pop(symbol, None)

    def accrue_interest(self, annual_rate: float, days: int) -> None:
        """闲置现金（扣除保证金占用的部分）按年化利率、自然日数计息。"""
        idle = self.cash - sum(self.margins.values())
        self.cash += idle * annual_rate * days / 365
