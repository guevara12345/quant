"""滚 IC 策略（不对冲）：持有当季 IC 多头，到期前 roll_days 个交易日展期到下一个季月合约。

当季合约 = 尚未进入展期窗口（距到期还剩超过 roll_days 个交易日）的最近一个季月合约。
建仓手数 = floor(现金 / 名义本金)，按 1 倍名义本金配置；展期时手数保持不变。
期货订单当日收盘价成交（ADR-0003），展期日由日历事先确定，不依赖未来行情。
数到期前的交易日数要求 DataFeed 的交易日历覆盖到合约到期日（CLI 用工作日把日历延伸到未来）。
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from quant.calendars import quarterly_contracts
from quant.instruments.future import expiry_of, is_future
from quant.strategy import Context, Order, Side, Strategy


class RollIC(Strategy):
    def __init__(self, multiplier: int, roll_days: int = 5):
        """multiplier 取 FutureConfig.multiplier，用来按名义本金算建仓手数。"""
        if roll_days < 0:
            raise ValueError(f"展期天数不能为负，实际 {roll_days}")
        self.roll_days = roll_days
        self.multiplier = multiplier

    def universe(self, day: date) -> list[str]:
        return [symbol for symbol, _, _ in quarterly_contracts("IC", day, 2)]

    def on_bar(self, ctx: Context) -> list[Order]:
        target = self.current_contract(ctx)
        held = {s: q for s, q in ctx.positions.items()
                if is_future(s)}
        if set(held) == {target}:
            return []
        bar = ctx.bar(target)
        if bar is None:
            return []
        lots = sum(held.values()) or math.floor(
            ctx.cash / (bar.close * self.multiplier))
        orders = [Order(s, Side.SELL) for s in held if s != target]
        if lots > held.get(target, 0):
            orders.append(Order(target, Side.BUY, lots - held.get(target, 0)))
        return orders

    def current_contract(self, ctx: Context) -> str:
        for symbol in self.universe(ctx.day):
            expiry = expiry_of(symbol, ctx.trading_days)
            if len(ctx.trading_days(ctx.day + timedelta(days=1), expiry)) > self.roll_days:
                return symbol
        raise RuntimeError(
            f"{ctx.day} 两个季月合约都在展期窗口内，交易日历可能没有覆盖到到期日")
