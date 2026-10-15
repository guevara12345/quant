"""DataFeed：按“日期 × 品种”提供 bar 与交易日历，是回测引擎唯一的数据入口（也是唯一的测试接缝）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True)
class Bar:
    """一个品种一个交易日的行情。open/high/low/close 为不复权价，adj_close 为后复权价。"""

    open: float
    high: float
    low: float
    close: float
    adj_close: float
    adj_factor: float
    suspended: bool
    is_st: bool


class DataFeed(Protocol):
    def trading_days(self, start: date, end: date) -> list[date]:
        """[start, end] 区间内的交易日，升序。"""
        ...

    def bar(self, symbol: str, day: date) -> Bar | None:
        """该品种在该交易日的 bar；没有数据时返回 None。"""
        ...


class InMemoryDataFeed:
    """内存中的 DataFeed，测试用手工构造的 bar 驱动引擎，真实数据适配器也最终落到它。"""

    def __init__(self, calendar: list[date], bars: dict[str, dict[date, Bar]]):
        self._calendar = sorted(calendar)
        self._bars = bars

    def trading_days(self, start: date, end: date) -> list[date]:
        return [d for d in self._calendar if start <= d <= end]

    def bar(self, symbol: str, day: date) -> Bar | None:
        return self._bars.get(symbol, {}).get(day)
