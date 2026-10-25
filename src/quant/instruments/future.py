"""期货品种规则（中金所股指期货）：当日收盘价加不利滑点成交（ADR-0003），每日按结算价盯市。

开仓不交割名义本金，只扣费用；盯市盈亏计入现金。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from quant.calendars import TradingDays, future_expiry
from quant.data.feed import Bar, DataFeed
from quant.portfolio import Alert, Fill, Portfolio, RejectReason, Rejection
from quant.strategy import Order, Side

# 中证 500 股指期货合约代码，如 IC2412：两位年份 + 两位月份。
# 只认 IC：FutureConfig 只有一个合约乘数，IF / IH（乘数 300）等品种要等配置按品种区分后再加
FUTURE_SYMBOL = re.compile(r"^IC(\d{2})(\d{2})$")


def is_future(symbol: str) -> bool:
    return FUTURE_SYMBOL.match(symbol) is not None


def expiry_of(symbol: str, trading_days: TradingDays) -> date:
    """合约到期日（最后交易日）。"""
    m = FUTURE_SYMBOL.match(symbol)
    if m is None:
        raise ValueError(f"不是股指期货合约代码：{symbol}")
    return future_expiry(2000 + int(m.group(1)), int(m.group(2)), trading_days)


@dataclass(frozen=True)
class FutureConfig:
    multiplier: int = 200  # IC 合约乘数，名义本金 = 价格 × 乘数
    margin_rate: float = 0.14  # 保证金占用 = 名义本金 × 保证金率，只记录不扣现金
    open_fee_rate: float = 0.000023  # 开仓，按成交金额
    close_yesterday_fee_rate: float = 0.000023  # 平昨仓
    close_today_fee_rate: float = 0.00023  # 平今仓
    slippage_points: float = 0.2  # 1 个最小变动价位，方向对交易者不利


class FutureRules:
    """期货的品种规则（InstrumentRules）。只支持多头：买入开仓、卖出平仓。"""

    fills_at_close = True

    def __init__(self, config: FutureConfig, feed: DataFeed):
        self.config = config
        self.feed = feed

    def quote(self, symbol: str, day: date) -> Bar | None:
        return self.feed.bar(symbol, day)

    def before_open(self, symbol: str, day: date, bar: Bar, portfolio: Portfolio) -> None:
        pass

    def match(self, order: Order, day: date, bar: Bar, portfolio: Portfolio) -> Fill | Rejection:
        """按当日收盘价加滑点成交。开仓要求现金覆盖全部持仓的保证金与费用；平仓数量不超过持仓。"""

        def reject(reason: RejectReason) -> Rejection:
            return Rejection(day, order.symbol, order.side, order.quantity, reason)

        sign = 1 if order.side is Side.BUY else -1
        price = bar.close + sign * self.config.slippage_points
        if order.side is Side.BUY:
            if order.quantity is None:
                raise ValueError(f"期货买入必须指定手数：{order}")
            fill = self._fill(order.symbol, order.side,
                              order.quantity, price, day, portfolio)
            margin = fill.amount * self.config.margin_rate
            if sum(portfolio.margins.values()) + margin + fill.fee > portfolio.cash:
                return reject(RejectReason.INSUFFICIENT_CASH)
            return fill
        held = portfolio.positions.get(order.symbol, 0)
        if held <= 0:
            return reject(RejectReason.NO_POSITION)
        quantity = held if order.quantity is None else min(
            order.quantity, held)
        return self._fill(order.symbol, order.side, quantity, price, day, portfolio)

    def _fill(self, symbol: str, side: Side, quantity: int, price: float, day: date, portfolio: Portfolio) -> Fill:
        """按成交金额收费：开仓与平昨同一费率，平今另一费率；平仓先平昨仓、后平今仓。"""
        c = self.config
        notional = price * c.multiplier
        if side is Side.BUY:
            fee = quantity * notional * c.open_fee_rate
        else:
            yesterday = min(quantity, portfolio.sellable(symbol, day))
            fee = notional * (yesterday * c.close_yesterday_fee_rate +
                              (quantity - yesterday) * c.close_today_fee_rate)
        return Fill(day, symbol, side, quantity, price, fee, c.multiplier)

    def apply(self, fill: Fill, portfolio: Portfolio) -> None:
        portfolio.apply_marked(fill)
        self._update_margin(fill.symbol, portfolio)

    def _update_margin(self, symbol: str, portfolio: Portfolio) -> None:
        """保证金占用 = 持仓 × 乘数 × 盯市基准价 × 保证金率；平仓后移除。"""
        if symbol not in portfolio.positions:
            portfolio.margins.pop(symbol, None)
        else:
            c = self.config
            portfolio.margins[symbol] = (abs(portfolio.positions[symbol]) * c.multiplier
                                         * portfolio.marks[symbol] * c.margin_rate)

    def after_close(
        self, symbol: str, day: date, bar: Bar | None, portfolio: Portfolio,
    ) -> tuple[list[Fill], list[Alert]]:
        """按结算价盯市，更新保证金占用；到期日仍有持仓时按结算价强制平仓并发出警告。"""
        if symbol not in portfolio.positions:
            return [], []
        c = self.config
        if bar is not None and bar.settle is not None:
            portfolio.mark_to_market(symbol, bar.settle, c.multiplier)
        if day >= expiry_of(symbol, self.feed.trading_days):
            held = portfolio.positions[symbol]
            fill = self._fill(symbol, Side.SELL, held,
                              portfolio.marks[symbol], day, portfolio)
            self.apply(fill, portfolio)
            return [fill], [Alert(day, symbol, f"持仓 {held} 手到期未展期，已按结算价 {fill.price} 强制平仓")]
        self._update_margin(symbol, portfolio)
        return [], []

    def market_value(self, symbol: str, quantity: int) -> float:
        """盯市盈亏已逐日计入现金，持仓本身不再计入净值。"""
        return 0.0
