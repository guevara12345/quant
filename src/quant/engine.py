"""按交易日推进的回测引擎。引擎本身不含品种特有逻辑（ADR-0001），撮合规则由品种规则负责。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

import pandas as pd

from quant.data.feed import Bar, DataFeed
from quant.instruments.stock import StockConfig, StockRules
from quant.portfolio import Fill, Portfolio
from quant.strategy import Context, Order, Strategy

FILL_COLUMNS = ["date", "symbol", "side", "quantity", "price", "fee"]


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 3_000_000.0
    cash_rate: float = 0.015  # 闲置现金年化利率，按自然日计息
    stock: StockConfig = field(default_factory=StockConfig)


@dataclass
class BacktestResult:
    daily: pd.DataFrame  # date, cash, market_value, nav, positions
    fills: pd.DataFrame  # FILL_COLUMNS


def run_backtest(
    feed: DataFeed,
    strategy: Strategy,
    symbol: str,
    start: date,
    end: date,
    config: BacktestConfig = BacktestConfig(),
) -> BacktestResult:
    portfolio = Portfolio(cash=config.initial_cash)
    rules = StockRules(config.stock)
    history: list[tuple[date, Bar]] = []
    last_close: dict[str, float] = {}
    pending: list[Order] = []
    fills: list[Fill] = []
    rows = []
    prev_day: date | None = None

    for day in feed.trading_days(start, end):
        bar = feed.bar(symbol, day)

        # 0. 现金计息：上一交易日收盘后的现金在两个交易日之间闲置，按间隔的自然日数计息
        if prev_day is not None:
            portfolio.accrue_interest(config.cash_rate, (day - prev_day).days)
        prev_day = day

        # 1. 撮合上一交易日收盘产生的股票订单（开盘价）
        if bar is not None:
            for order in pending:
                fill = rules.match_at_open(order, day, bar, portfolio)
                if fill is not None:
                    portfolio.apply(fill)
                    fills.append(fill)
            pending = []
            history.append((day, bar))
            last_close[symbol] = bar.close

        # 2. 收盘调用策略，订单在下一交易日开盘撮合
        if bar is not None:
            ctx = Context(day=day, symbol=symbol, history=list(history),
                          cash=portfolio.cash, positions=dict(portfolio.positions))
            pending.extend(strategy.on_bar(ctx))

        # 3. 记录当日快照
        market_value = sum(q * last_close[s] for s, q in portfolio.positions.items())
        rows.append({
            "date": day,
            "cash": portfolio.cash,
            "market_value": market_value,
            "nav": portfolio.cash + market_value,
            "positions": dict(portfolio.positions),
        })

    fill_rows = [{**asdict(f), "side": f.side.value} for f in fills]
    return BacktestResult(daily=pd.DataFrame(rows), fills=pd.DataFrame(fill_rows, columns=FILL_COLUMNS))
