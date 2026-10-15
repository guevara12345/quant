"""策略接口：引擎在每个交易日收盘时调用策略，策略返回订单。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from enum import Enum

from quant.data.feed import Bar


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Order:
    """quantity 为 None 时：买入表示满仓买入，卖出表示全部卖出，具体股数在成交时确定。"""

    symbol: str
    side: Side
    quantity: int | None = None


@dataclass(frozen=True)
class Context:
    """策略在信号日收盘时能看到的全部信息；history 只包含截至当日（含）的 bar，不含未来数据。"""

    day: date
    symbol: str
    history: list[tuple[date, Bar]]
    cash: float
    positions: dict[str, int]

    @property
    def bar(self) -> Bar:
        return self.history[-1][1]


class Strategy(ABC):
    @abstractmethod
    def on_bar(self, ctx: Context) -> list[Order]: ...
