"""策略接口：引擎在每个交易日收盘时调用策略，策略返回订单。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from quant.data.feed import Bar


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Order:
    """quantity 为 None 时：买入表示满仓买入，卖出表示全部卖出，具体数量在成交时确定。"""

    symbol: str
    side: Side
    quantity: int | None = None

    def __post_init__(self) -> None:
        if self.quantity is not None and self.quantity <= 0:
            raise ValueError(f"订单数量必须为正数，实际 {self.quantity}")


@dataclass(frozen=True)
class Context:
    """策略在信号日收盘时能看到的全部信息，不含未来数据。

    bars 是当日 universe 内各品种的 bar（无数据的品种不在其中）；bar(symbol) 对 universe 之外的品种按需现取当日行情
    （如收盘时才知道行权价的 put），同样不含未来数据。history(symbol) 只包含截至当日（含）的 bar。
    交易日历是事先公布的公开信息，可以查询未来日期（trading_days），用于计算到期前的交易日数。
    """

    day: date
    cash: float
    positions: dict[str, int]
    bars: dict[str, Bar]
    _history: dict[str, list[tuple[date, Bar]]] = field(repr=False)
    _trading_days: Callable[[date, date], list[date]] = field(repr=False)
    _quote: Callable[[str], Bar | None] = field(default=lambda symbol: None, repr=False)

    def bar(self, symbol: str) -> Bar | None:
        return self.bars[symbol] if symbol in self.bars else self._quote(symbol)

    def history(self, symbol: str) -> list[tuple[date, Bar]]:
        return list(self._history.get(symbol, []))

    def trading_days(self, start: date, end: date) -> list[date]:
        return self._trading_days(start, end)


class Strategy(ABC):
    @abstractmethod
    def universe(self, day: date) -> list[str]:
        """当日需要行情的品种；引擎另外会带上持仓中的品种。"""

    @abstractmethod
    def on_bar(self, ctx: Context) -> list[Order]: ...
