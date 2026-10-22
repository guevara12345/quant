"""品种规则：引擎按品种分派到这里的规则对象，自身不含品种特有逻辑（ADR-0001）。"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from quant.data.feed import Bar
from quant.portfolio import Alert, Fill, Portfolio, Rejection
from quant.strategy import Order


class InstrumentRules(Protocol):
    """一类品种的交易规则。每次回测新建实例，可以按品种保存状态（如涨跌停基准价）。"""

    # False：订单进 pending、次一交易日开盘撮合（股票）；True：策略返回后当日收盘撮合（衍生品，ADR-0003）
    fills_at_close: bool

    def before_open(self, symbol: str, day: date, bar: Bar, portfolio: Portfolio) -> None:
        """开盘前的调整，如股票除权。"""
        ...

    def match(self, order: Order, day: date, bar: Bar, portfolio: Portfolio) -> Fill | Rejection:
        """按本品种的成交时点撮合，返回成交或带原因的未成交记录。"""
        ...

    def apply(self, fill: Fill, portfolio: Portfolio) -> None:
        """把成交记入账户（现金交割或逐日盯市）。"""
        ...

    def after_close(
        self, symbol: str, day: date, bar: Bar | None, portfolio: Portfolio,
    ) -> tuple[list[Fill], list[Alert]]:
        """收盘后的处理（盯市、到期），返回已记入账户的强制成交和警告。bar 为 None 表示当日无行情。"""
        ...

    def market_value(self, symbol: str, quantity: int) -> float:
        """持仓计入净值的价值。"""
        ...
