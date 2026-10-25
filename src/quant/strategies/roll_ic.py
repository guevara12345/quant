"""滚 IC 策略：持有当季 IC 多头，到期前 roll_days 个交易日展期到下一个季月合约；可选用 500ETF put 对冲到 delta 中性。

当季合约 = 尚未进入展期窗口（距到期还剩超过 roll_days 个交易日）的最近一个季月合约。
建仓手数 = floor(现金 / 名义本金)，按 1 倍名义本金配置；展期时手数保持不变。
期货、期权订单当日收盘价 / 模型价成交（ADR-0003），展期日由日历事先确定，不依赖未来行情。
数到期前的交易日数要求 DataFeed 的交易日历覆盖到合约到期日（CLI 用工作日把日历延伸到未来）。

对冲（hedge 不为 None）：每日收盘在 IC 订单之后算组合净 delta（都按名义本金折算，单位元）。
建仓、put 换月（持有的 put 到期前 roll_days 个交易日），以及 |净 delta| 超过 IC 名义本金 × threshold 时，
按同一条规则重选 put：平值（行权价网格上不高于 ETF 收盘价的最近一档）、次月（当日所在月份的下一个月）。
重选的合约就是持有的 put 时只调张数，否则平掉旧 put、买入新 put；张数使净 delta 最接近 0。
再平衡不沿用持有的行权价和月份：ETF 大涨后旧 put 深度虚值，临近到期的 put 偏虚值一档，delta 都趋近 0，张数会失控。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from quant.calendars import TradingDays, option_expiry, quarterly_contracts, strike_at_or_below
from quant.data.feed import Bar
from quant.instruments.future import expiry_of, is_future
from quant.instruments.option import OptionConfig, delta_exposure, is_option, option_symbol, parse_option
from quant.strategy import Context, Order, Side, Strategy


@dataclass(frozen=True)
class PutHedge:
    option: OptionConfig  # 取 BacktestConfig.option：乘数、利率、股息率，用来算 put 的 delta
    threshold: float = 0.10  # |净 delta| 超过 IC 名义本金的这个比例时再平衡；0 即每日再平衡
    roll_days: int = 5  # put 到期前多少个交易日换到新的次月合约
    underlying: str = "sh.510500"


class RollIC(Strategy):
    def __init__(self, multiplier: int, roll_days: int = 5, hedge: PutHedge | None = None):
        """multiplier 取 FutureConfig.multiplier，用来按名义本金算建仓手数和 delta。hedge 为 None 时不对冲。"""
        if roll_days < 0 or (hedge is not None and hedge.roll_days < 0):
            raise ValueError(f"展期 / 换月天数不能为负，实际 {roll_days}、{hedge and hedge.roll_days}")
        self.roll_days = roll_days
        self.multiplier = multiplier
        self.hedge = hedge

    def universe(self, day: date) -> list[str]:
        symbols = [symbol for symbol, _, _ in quarterly_contracts("IC", day, 2)]
        return symbols + ([self.hedge.underlying] if self.hedge else [])

    def on_bar(self, ctx: Context) -> list[Order]:
        target = self.current_contract(ctx)
        held = {s: q for s, q in ctx.positions.items()
                if is_future(s)}
        bar = ctx.bar(target)
        if bar is None:
            return []
        lots = sum(held.values()) or math.floor(
            ctx.cash / (bar.close * self.multiplier))
        orders = []
        if set(held) != {target}:
            orders = [Order(s, Side.SELL) for s in held if s != target]
            if lots > held.get(target, 0):
                orders.append(Order(target, Side.BUY, lots - held.get(target, 0)))
        if self.hedge is not None and lots > 0:
            orders += self.hedge_orders(ctx, lots * self.multiplier * bar.close)
        return orders

    def current_contract(self, ctx: Context) -> str:
        for symbol in self.universe(ctx.day):
            if not is_future(symbol):
                continue
            expiry = expiry_of(symbol, ctx.trading_days)
            if len(ctx.trading_days(ctx.day + timedelta(days=1), expiry)) > self.roll_days:
                return symbol
        raise RuntimeError(
            f"{ctx.day} 两个季月合约都在展期窗口内，交易日历可能没有覆盖到到期日")

    def hedge_orders(self, ctx: Context, ic_delta: float) -> list[Order]:
        """建仓、换月或净 delta 超过阈值时，重选次月平值 put，张数使 ic_delta + 张数 × 每张 delta 敞口最接近 0（ADR-0004）。

        标的或要买的 put 无行情时不动。
        """
        assert self.hedge is not None
        h = self.hedge
        etf = ctx.bar(h.underlying)
        if etf is None:
            return []
        held = {s: q for s, q in ctx.positions.items() if is_option(s)}
        if (len(held) == 1 and not self._put_expiring(ctx, current := next(iter(held)))
                and not self._exceeds_threshold(ctx, current, held[current], etf.close, ic_delta)):
            return []
        year, month = (ctx.day.year + 1, 1) if ctx.day.month == 12 else (ctx.day.year, ctx.day.month + 1)
        put = option_symbol(h.underlying, year, month, strike_at_or_below(etf.close))
        quote = ctx.bar(put)
        if quote is None:
            return []
        per_contract = delta_exposure(put, ctx.day, quote.close, etf.close, h.option, ctx.trading_days)
        if per_contract >= 0:
            return []
        orders = [Order(s, Side.SELL) for s in held if s != put]
        diff = round(ic_delta / -per_contract) - held.get(put, 0)
        if diff > 0:
            orders.append(Order(put, Side.BUY, diff))
        elif diff < 0:
            orders.append(Order(put, Side.SELL, -diff))
        return orders

    def _exceeds_threshold(self, ctx: Context, put: str, quantity: int, spot: float, ic_delta: float) -> bool:
        """|ic_delta + put 张数 × 每张 delta 敞口| 是否超过 IC 名义本金 × 阈值；put 无行情时视为未超过。"""
        assert self.hedge is not None
        quote = ctx.bar(put)
        if quote is None:
            return False
        per_contract = delta_exposure(put, ctx.day, quote.close, spot, self.hedge.option, ctx.trading_days)
        return abs(ic_delta + quantity * per_contract) > self.hedge.threshold * ic_delta

    def _put_expiring(self, ctx: Context, symbol: str) -> bool:
        assert self.hedge is not None
        c = parse_option(symbol)
        expiry = option_expiry(c.year, c.month, ctx.trading_days)
        return len(ctx.trading_days(ctx.day + timedelta(days=1), expiry)) <= self.hedge.roll_days


def net_delta(positions: dict[str, int], day: date, quote: Callable[[str], Bar | None], multiplier: int,
              hedge: PutHedge, trading_days: TradingDays) -> float:
    """组合净 delta（元）：IC 手数 × 乘数 × IC 收盘价 + put 张数 × 每张 delta 敞口。缺行情的品种不计。"""
    etf = quote(hedge.underlying)
    total = 0.0
    for symbol, quantity in positions.items():
        bar = quote(symbol)
        if bar is None:
            continue
        if is_future(symbol):
            total += quantity * multiplier * bar.close
        elif is_option(symbol) and etf is not None:
            total += quantity * delta_exposure(symbol, day, bar.close, etf.close, hedge.option, trading_days)
    return total
