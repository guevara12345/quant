"""按交易日推进的回测引擎。引擎本身不含品种特有逻辑（ADR-0001），按品种分派给品种规则。"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from functools import partial

import pandas as pd

from quant.data.feed import Bar, DataFeed
from quant.instruments import InstrumentRules
from quant.instruments.future import FutureConfig, FutureRules, is_future
from quant.instruments.option import OptionConfig, OptionRules, is_option
from quant.instruments.stock import StockConfig, StockRules
from quant.portfolio import Alert, Fill, Portfolio, RejectReason, Rejection
from quant.strategy import Context, Order, Strategy

log = logging.getLogger(__name__)

FILL_COLUMNS = ["date", "symbol", "side",
                "quantity", "price", "fee", "multiplier"]
REJECTION_COLUMNS = ["date", "symbol", "side", "quantity", "reason"]
ALERT_COLUMNS = ["date", "symbol", "message"]


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 3_000_000.0
    cash_rate: float = 0.015  # 闲置现金年化利率，按自然日计息
    stock: StockConfig = field(default_factory=StockConfig)
    future: FutureConfig = field(default_factory=FutureConfig)
    option: OptionConfig = field(default_factory=OptionConfig)


@dataclass
class BacktestResult:
    daily: pd.DataFrame  # date, cash, market_value, margin, nav, interest（当日计入的利息）, positions
    fills: pd.DataFrame  # FILL_COLUMNS
    rejections: pd.DataFrame  # REJECTION_COLUMNS，未成交订单及原因
    alerts: pd.DataFrame  # ALERT_COLUMNS，如到期强制平仓


class _Dispatcher:
    """按品种代码选规则对象。"""

    def __init__(self, config: BacktestConfig, feed: DataFeed):
        self.stock = StockRules(config.stock, feed)
        self.future = FutureRules(config.future, feed)
        self.option = OptionRules(config.option, feed)

    def __call__(self, symbol: str) -> InstrumentRules:
        if is_future(symbol):
            return self.future
        return self.option if is_option(symbol) else self.stock

    def quote(self, day: date, symbol: str) -> Bar | None:
        return self(symbol).quote(symbol, day)


def run_backtest(
    feed: DataFeed,
    strategy: Strategy,
    start: date,
    end: date,
    config: BacktestConfig = BacktestConfig(),
) -> BacktestResult:
    portfolio = Portfolio(cash=config.initial_cash)
    rules_for = _Dispatcher(config, feed)
    history: dict[str, list[tuple[date, Bar]]] = {}
    pending: list[Order] = []
    fills: list[Fill] = []
    rejections: list[Rejection] = []
    alerts: list[Alert] = []
    rows = []
    prev_day: date | None = None

    def execute(order: Order, day: date, bar: Bar) -> None:
        rules = rules_for(order.symbol)
        outcome = rules.match(order, day, bar, portfolio)
        if isinstance(outcome, Fill):
            rules.apply(outcome, portfolio)
            fills.append(outcome)
        else:
            rejections.append(outcome)

    for day in feed.trading_days(start, end):
        symbols = list(dict.fromkeys(
            [*strategy.universe(day), *portfolio.positions, *(o.symbol for o in pending)]))
        bars = {s: b for s in symbols if (b := rules_for(s).quote(s, day)) is not None}

        # 0. 现金计息：上一交易日收盘后的闲置现金（扣除保证金占用）按间隔的自然日数计息
        interest = 0.0
        if prev_day is not None:
            interest = portfolio.accrue_interest(config.cash_rate, (day - prev_day).days)
        prev_day = day

        # 1. 开盘前调整（股票除权：持股按复权因子放大、涨跌停改以除权参考价为基准）
        for s, bar in bars.items():
            rules_for(s).before_open(s, day, bar, portfolio)

        # 2. 撮合上一交易日收盘产生的开盘成交订单；当日无行情的品种，订单留到下一个有行情的交易日
        waiting = []
        for order in pending:
            if order.symbol in bars:
                execute(order, day, bars[order.symbol])
            else:
                waiting.append(order)
        pending = waiting
        for s, bar in bars.items():
            history.setdefault(s, []).append((day, bar))

        # 3. 收盘调用策略；开盘成交的订单进 pending，收盘成交的订单当日撮合（ADR-0003）
        if bars:
            ctx = Context(day=day, cash=portfolio.cash, positions=dict(portfolio.positions),
                          bars=dict(bars), _history=history, _trading_days=feed.trading_days,
                          _quote=partial(rules_for.quote, day))
            for order in strategy.on_bar(ctx):
                if not rules_for(order.symbol).fills_at_close:
                    pending.append(order)
                elif (quote := bars.get(order.symbol) or rules_for(order.symbol).quote(order.symbol, day)) is not None:
                    execute(order, day, quote)
                else:
                    rejections.append(Rejection(
                        day, order.symbol, order.side, order.quantity, RejectReason.NO_QUOTE))

        # 4. 收盘后处理：盯市、到期强制平仓等
        for s in dict.fromkeys([*bars, *portfolio.positions]):
            forced, warned = rules_for(s).after_close(
                s, day, bars.get(s), portfolio)
            fills.extend(forced)
            alerts.extend(warned)
            for alert in warned:
                log.warning("%s %s：%s", alert.date,
                            alert.symbol, alert.message)

        # 5. 记录当日快照
        market_value = sum(rules_for(s).market_value(s, q)
                           for s, q in portfolio.positions.items())
        rows.append({
            "date": day,
            "cash": portfolio.cash,
            "market_value": market_value,
            "margin": sum(portfolio.margins.values()),
            "nav": portfolio.cash + market_value,
            "interest": interest,
            "positions": dict(portfolio.positions),
        })

    fill_rows = [{**asdict(f), "side": f.side.value} for f in fills]
    rejection_rows = [
        {**asdict(r), "side": r.side.value, "reason": r.reason.value} for r in rejections]
    return BacktestResult(
        daily=pd.DataFrame(rows),
        fills=pd.DataFrame(fill_rows, columns=FILL_COLUMNS),
        rejections=pd.DataFrame(rejection_rows, columns=REJECTION_COLUMNS),
        alerts=pd.DataFrame([asdict(a)
                            for a in alerts], columns=ALERT_COLUMNS),
    )
