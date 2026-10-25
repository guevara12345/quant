"""ETF 期权品种规则（上交所 500ETF 期权）：按 BS 模型价当日收盘成交与估值（ADR-0002、ADR-0003），到期按内在价值结算。

期权没有自己的行情：当日 bar 由标的 ETF 的收盘价与已实现波动率现算（quote），开高低收都等于模型价。
买方不交保证金，权利金现金交割，每张收固定费用。只支持买方：买入开仓、卖出平仓。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from quant.calendars import TradingDays, option_expiry
from quant.data.feed import Bar, DataFeed
from quant.portfolio import Alert, Fill, Portfolio, RejectReason, Rejection
from quant.pricing import implied_vol, put_delta, put_price, realized_vol
from quant.strategy import Order, Side

# 上交所期权合约交易代码，如 510500P2412M05500：标的代码 + P + 两位年份 + 两位月份 + M + 行权价（厘，5 位）。
# 只认 put：call 的模型价与 delta 等到有策略需要时再加
OPTION_SYMBOL = re.compile(r"^(\d{6})P(\d{2})(\d{2})M(\d{5})$")


def is_option(symbol: str) -> bool:
    return OPTION_SYMBOL.match(symbol) is not None


@dataclass(frozen=True)
class OptionContract:
    underlying: str  # 标的 ETF 在 DataFeed 中的代码，如 "sh.510500"
    year: int
    month: int
    strike: float


def parse_option(symbol: str) -> OptionContract:
    m = OPTION_SYMBOL.match(symbol)
    if m is None:
        raise ValueError(f"不是 ETF 期权合约代码：{symbol}")
    code, yy, mm, strike = m.groups()
    # 上交所 ETF 用 baostock 风格的 sh. 前缀，与指数、股票一致
    return OptionContract(f"sh.{code}", 2000 + int(yy), int(mm), int(strike) / 1000)


def option_symbol(underlying: str, year: int, month: int, strike: float) -> str:
    """parse_option 的逆：("sh.510500", 2024, 3, 5.5) → "510500P2403M05500"。"""
    return f"{underlying.split('.')[-1]}P{year % 100:02d}{month:02d}M{round(strike * 1000):05d}"


@dataclass(frozen=True)
class OptionConfig:
    multiplier: int = 10000  # 合约乘数，权利金 = 模型价 × 乘数
    fee_per_contract: float = 2.0  # 每张费用，买卖都收；到期结算不收
    risk_free_rate: float = 0.015
    dividend_yield: float = 0.015
    vol_window: int = 20  # 已实现波动率的窗口（日收益率个数）
    vol_premium: float = 1.1  # 模型波动率 = 已实现波动率 × 溢价系数

    @property
    def vol_lookback_days(self) -> int:
        """算波动率要往前看的自然日数：3 倍窗口再加 10 天，足够覆盖春节等长假。"""
        return 3 * self.vol_window + 10


def delta_exposure(symbol: str, day: date, price: float, spot: float, config: OptionConfig,
                   trading_days: TradingDays) -> float:
    """每张 put 的 delta 敞口（元，≤ 0）= 乘数 × 标的价 × delta。

    波动率由当日价格反推，使用者不必知道模型价用的波动率；剩余期限与 quote 一致，按自然日 / 365。
    """
    c = parse_option(symbol)
    years = (option_expiry(c.year, c.month, trading_days) - day).days / 365
    r, q = config.risk_free_rate, config.dividend_yield
    vol = implied_vol(price, spot, c.strike, years, r, q) if years > 0 else 0.0
    return config.multiplier * spot * put_delta(spot, c.strike, years, r, q, vol)


class OptionRules:
    """期权的品种规则（InstrumentRules）。按品种记录最近一次模型价，用于估值和到期结算。"""

    fills_at_close = True

    def __init__(self, config: OptionConfig, feed: DataFeed):
        self.config = config
        self.feed = feed
        self._marks: dict[str, float] = {}
        self._expiry: dict[str, date] = {}
        self._vol: dict[tuple[str, date], float | None] = {}

    def expiry(self, symbol: str) -> date:
        if symbol not in self._expiry:
            c = parse_option(symbol)
            self._expiry[symbol] = option_expiry(c.year, c.month, self.feed.trading_days)
        return self._expiry[symbol]

    def quote(self, symbol: str, day: date) -> Bar | None:
        """当日模型价：标的当日不复权收盘价、剩余自然日 / 365、截至当日的已实现波动率 × 溢价系数。

        已过到期日、标的当日无行情，或到期前历史不足一个波动率窗口时，返回 None（当日无报价）。
        """
        c, cfg = parse_option(symbol), self.config
        etf = self.feed.bar(c.underlying, day)
        years = (self.expiry(symbol) - day).days / 365
        if etf is None or years < 0:
            return None
        vol = self.model_vol(c.underlying, day)
        if vol is None and years > 0:
            return None
        price = put_price(etf.close, c.strike, years, cfg.risk_free_rate, cfg.dividend_yield, vol or 0.0)
        return Bar(open=price, high=price, low=price, close=price, adj_close=price, adj_factor=1.0,
                   suspended=False, is_st=False)

    def model_vol(self, underlying: str, day: date) -> float | None:
        """截至 day（含）最近 vol_window 个日收益率的已实现波动率 × 溢价系数；用后复权价，避免分红缺口。"""
        key = (underlying, day)
        if key not in self._vol:
            n = self.config.vol_window
            days = self.feed.trading_days(day - timedelta(days=self.config.vol_lookback_days), day)
            closes = [b.adj_close for d in days if (b := self.feed.bar(underlying, d)) is not None][-(n + 1):]
            self._vol[key] = realized_vol(closes) * self.config.vol_premium if len(closes) == n + 1 else None
        return self._vol[key]

    def before_open(self, symbol: str, day: date, bar: Bar, portfolio: Portfolio) -> None:
        pass

    def match(self, order: Order, day: date, bar: Bar, portfolio: Portfolio) -> Fill | Rejection:
        """按当日模型价成交。买入要求现金覆盖权利金与费用（不动用保证金占用）；卖出数量不超过持仓。"""

        def reject(reason: RejectReason) -> Rejection:
            return Rejection(day, order.symbol, order.side, order.quantity, reason)

        c = self.config
        if order.side is Side.BUY:
            if order.quantity is None:
                raise ValueError(f"期权买入必须指定张数：{order}")
            fill = Fill(day, order.symbol, order.side, order.quantity, bar.close,
                        order.quantity * c.fee_per_contract, c.multiplier)
            if fill.amount + fill.fee > portfolio.cash - sum(portfolio.margins.values()):
                return reject(RejectReason.INSUFFICIENT_CASH)
            return fill
        held = portfolio.positions.get(order.symbol, 0)
        if held <= 0:
            return reject(RejectReason.NO_POSITION)
        quantity = held if order.quantity is None else min(order.quantity, held)
        return Fill(day, order.symbol, order.side, quantity, bar.close, quantity * c.fee_per_contract,
                    c.multiplier)

    def apply(self, fill: Fill, portfolio: Portfolio) -> None:
        portfolio.apply(fill)
        self._marks[fill.symbol] = fill.price

    def after_close(
        self, symbol: str, day: date, bar: Bar | None, portfolio: Portfolio,
    ) -> tuple[list[Fill], list[Alert]]:
        """按当日模型价估值；到期日仍有持仓时按内在价值（到期日模型价）卖出结算，不收费用，并发出警告。

        到期日标的缺行情时只能沿用最近一次模型价结算，警告里注明。
        """
        if symbol not in portfolio.positions:
            return [], []
        if bar is not None:
            self._marks[symbol] = bar.close
        if day < self.expiry(symbol):
            return [], []
        held = portfolio.positions[symbol]
        fill = Fill(day, symbol, Side.SELL, held, self._marks[symbol], 0.0, self.config.multiplier)
        self.apply(fill, portfolio)
        basis = "内在价值" if bar is not None else "最近一次模型价（标的当日无行情）"
        return [fill], [Alert(day, symbol, f"持仓 {held} 张到期，已按{basis} {fill.price:.4f} 结算")]

    def market_value(self, symbol: str, quantity: int) -> float:
        """持仓 × 最近一次模型价 × 乘数。"""
        return quantity * self._marks[symbol] * self.config.multiplier
