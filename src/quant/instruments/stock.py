"""股票品种规则：成交日按开盘价（不复权）加不利滑点撮合，遵循 A 股交易约束。

检查顺序：停牌 → 涨跌停（开盘价等于涨停价时跳过买单、等于跌停价时跳过卖单）→
买入按整手取整并检查资金 / 卖出检查持仓与 T+1。没有成交的订单返回 Rejection 并注明原因。
费用为佣金（有最低额）+ 过户费，卖出另收印花税。除权日持股数按复权因子比例放大、涨跌停以除权参考价为基准（apply_ex_rights）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from quant.data.feed import Bar
from quant.portfolio import Alert, Fill, Portfolio, RejectReason, Rejection
from quant.strategy import Order, Side

# 按代码前缀判断板块（代码形如 sz.300750，取点号后的部分）
CHINEXT_PREFIXES = ("300", "301")  # 深市创业板
STAR_PREFIXES = ("688", "689")  # 沪市科创板


@dataclass(frozen=True)
class StockConfig:
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_duty_rate: float = 0.0005  # 印花税，只在卖出时收取
    transfer_fee_rate: float = 0.00001  # 过户费，买卖双向
    slippage: float = 0.001  # 按开盘价的比例，方向对交易者不利
    lot_size: int = 100  # 整手：买入股数向下取整到它的整数倍
    # 涨跌幅限制，相对前收盘价
    main_board_limit: float = 0.10
    chinext_limit: float = 0.20  # 创业板注册制改革之后
    chinext_reform_date: date = date(2020, 8, 24)  # 之前创业板与主板相同
    star_limit: float = 0.20
    st_limit: float = 0.05  # 只作用于主板（及改革前的创业板），创业板、科创板的 ST 股与板块相同


class StockRules:
    """股票的品种规则（InstrumentRules）。按品种记录涨跌停基准价与复权因子，每次回测新建一个实例。"""

    fills_at_close = False  # 订单进 pending，次一交易日开盘撮合

    def __init__(self, config: StockConfig):
        self.config = config
        # 涨跌停基准：上一个有 bar 的交易日的不复权收盘价，除权日替换为除权参考价
        self._ref_price: dict[str, float] = {}
        self._last_factor: dict[str, float] = {}

    def limit_ratio(self, symbol: str, day: date, is_st: bool) -> float:
        """当日涨跌幅限制：按板块、是否 ST、创业板改革日期决定。"""
        code = symbol.rsplit(".", 1)[-1]
        if code.startswith(STAR_PREFIXES):
            return self.config.star_limit
        if code.startswith(CHINEXT_PREFIXES) and day >= self.config.chinext_reform_date:
            return self.config.chinext_limit
        return self.config.st_limit if is_st else self.config.main_board_limit

    def fee(self, side: Side, amount: float) -> float:
        """交易费用：佣金（有最低额）+ 过户费，卖出另收印花税。"""
        if amount <= 0:
            return 0.0
        c = self.config
        fee = max(amount * c.commission_rate, c.min_commission) + \
            amount * c.transfer_fee_rate
        if side is Side.SELL:
            fee += amount * c.stamp_duty_rate
        return fee

    def before_open(self, symbol: str, day: date, bar: Bar, portfolio: Portfolio) -> None:
        """开盘前：复权因子较上一个有 bar 的交易日变化时做除权调整。"""
        prev_factor = self._last_factor.get(symbol)
        if prev_factor is not None and bar.adj_factor != prev_factor:
            self._ref_price[symbol] = self.apply_ex_rights(
                symbol, bar.adj_factor / prev_factor, self._ref_price[symbol], portfolio)
        self._last_factor[symbol] = bar.adj_factor

    def apply_ex_rights(self, symbol: str, ratio: float, prev_close: float, portfolio: Portfolio) -> float:
        """除权日开盘前调用；ratio = 当日复权因子 / 前一日复权因子。

        持股数按 ratio 放大，取最近的整股、不要求整手（零股可以卖出），近似分红再投资。
        返回除权参考价 = 前收盘价 / ratio，四舍五入到分，作为当日涨跌停基准；
        它由复权因子比例推算，是交易所公布值的近似。
        """
        held = portfolio.positions.get(symbol, 0)
        if held:
            portfolio.adjust_position(symbol, round(held * ratio))
        return _to_cent(prev_close / ratio)

    def match(self, order: Order, day: date, bar: Bar, portfolio: Portfolio) -> Fill | Rejection:
        return self.match_at_open(order, day, bar, self._ref_price.get(order.symbol), portfolio)

    def apply(self, fill: Fill, portfolio: Portfolio) -> None:
        portfolio.apply(fill)

    def after_close(
        self, symbol: str, day: date, bar: Bar | None, portfolio: Portfolio,
    ) -> tuple[list[Fill], list[Alert]]:
        """收盘后记录收盘价，作为估值价和下一交易日的涨跌停基准。"""
        if bar is not None:
            self._ref_price[symbol] = bar.close
        return [], []

    def market_value(self, symbol: str, quantity: int) -> float:
        return quantity * self._ref_price[symbol]

    def match_at_open(
        self, order: Order, day: date, bar: Bar, prev_close: float | None, portfolio: Portfolio,
    ) -> Fill | Rejection:
        """按成交日开盘价撮合；prev_close 为涨跌停基准（上一交易日不复权收盘价，除权日为除权参考价），没有时不检查涨跌停。"""

        def reject(reason: RejectReason) -> Rejection:
            return Rejection(day, order.symbol, order.side, order.quantity, reason)

        if bar.suspended:
            return reject(RejectReason.SUSPENDED)
        if prev_close is not None:
            ratio = self.limit_ratio(order.symbol, day, bar.is_st)
            if order.side is Side.BUY and bar.open >= limit_price(prev_close, 1 + ratio):
                return reject(RejectReason.LIMIT_UP)
            if order.side is Side.SELL and bar.open <= limit_price(prev_close, 1 - ratio):
                return reject(RejectReason.LIMIT_DOWN)
        sign = 1 if order.side is Side.BUY else -1
        price = bar.open * (1 + sign * self.config.slippage)
        if order.side is Side.BUY:
            if order.quantity is None:
                quantity = self._max_affordable(
                    price, portfolio.cash)
                if quantity <= 0:
                    return reject(RejectReason.INSUFFICIENT_CASH)
            else:
                quantity = self._round_lot(order.quantity)
                if quantity <= 0:
                    return reject(RejectReason.ODD_LOT)
            if quantity * price + self.fee(Side.BUY, quantity * price) > portfolio.cash:
                return reject(RejectReason.INSUFFICIENT_CASH)
        else:
            if portfolio.positions.get(order.symbol, 0) <= 0:
                return reject(RejectReason.NO_POSITION)
            sellable = portfolio.sellable(order.symbol, day)
            if sellable <= 0:
                return reject(RejectReason.T_PLUS_1)
            # 指定数量超过可卖数量时只卖出可卖部分
            quantity = sellable if order.quantity is None else min(
                order.quantity, sellable)
        return Fill(day, order.symbol, order.side, quantity, price, self.fee(order.side, quantity * price))

    def _round_lot(self, quantity: int) -> int:
        return quantity // self.config.lot_size * self.config.lot_size

    def _max_affordable(self, price: float, cash: float) -> int:
        """满仓买入：现金（含费用）能买到的最大整手股数，从不计费用时的上限逐手递减。"""
        lot = self.config.lot_size
        quantity = self._round_lot(math.floor(cash / price))
        while quantity > 0 and quantity * price + self.fee(Side.BUY, quantity * price) > cash:
            quantity -= lot
        return quantity


def limit_price(prev_close: float, multiplier: float) -> float:
    """涨跌停价：前收盘价 × (1 ± 涨跌幅)，四舍五入到分。"""
    return _to_cent(Decimal(str(prev_close)) * Decimal(str(multiplier)))


def _to_cent(price: float | Decimal) -> float:
    return float(Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
