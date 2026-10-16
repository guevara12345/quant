"""绩效指标与买入持有基准。"""

from __future__ import annotations

import math

import pandas as pd

from quant.data.feed import DataFeed
from quant.engine import BacktestResult
from quant.strategy import Side

TRADING_DAYS_PER_YEAR = 252


def buy_and_hold_nav(
        feed: DataFeed, symbol: str, days: list, initial_cash: float) -> pd.Series:
    """基准：首日按后复权收盘价全额持有到最后一日，不计费用。"""
    prices = pd.Series({d: bar.adj_close for d in days if (
        bar := feed.bar(symbol, d)) is not None})
    return initial_cash * prices / prices.iloc[0]


def compute_metrics(
        result: BacktestResult, feed: DataFeed, symbol: str, risk_free_rate: float = 0.015) -> dict:
    nav = result.daily.set_index("date")["nav"]
    initial = nav.iloc[0]
    benchmark = buy_and_hold_nav(feed, symbol, list(nav.index), initial)
    years = (nav.index[-1] - nav.index[0]).days / 365

    fills = result.fills
    traded_amount = (fills["quantity"] * fills["price"]).sum()
    wins, trades = _round_trip_wins(fills)

    metrics = {
        "total_return": nav.iloc[-1] / initial - 1,
        "annual_return": _annualize(nav, years),
        "max_drawdown": _max_drawdown(nav),
        "sharpe": _sharpe(nav, risk_free_rate),
        "win_rate": wins / trades if trades else float("nan"),
        "trade_count": trades,
        # 年化换手率：单边成交额 / 平均净值 / 年数
        "turnover": traded_amount / 2 / nav.mean() / years if years > 0 else float("nan"),
        "total_fees": fills["fee"].sum(),
        "benchmark_total_return": benchmark.iloc[-1] / benchmark.iloc[0] - 1,
        "benchmark_annual_return": _annualize(benchmark, years),
    }
    metrics["excess_annual_return"] = metrics["annual_return"] - \
        metrics["benchmark_annual_return"]
    return metrics


def _annualize(nav: pd.Series, years: float) -> float:
    if years <= 0:
        return float("nan")
    return (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1


def _max_drawdown(nav: pd.Series) -> float:
    return float((1 - nav / nav.cummax()).max())


def _sharpe(nav: pd.Series, risk_free_rate: float) -> float:
    excess = nav.pct_change().dropna() - risk_free_rate / TRADING_DAYS_PER_YEAR
    std = excess.std()
    if len(excess) < 2 or not std > 0:
        return float("nan")
    return float(excess.mean() / std * math.sqrt(TRADING_DAYS_PER_YEAR))


def _round_trip_wins(fills: pd.DataFrame) -> tuple[int, int]:
    """每笔卖出算一次交易：卖出所得（扣费）减去按平均成本（含买入费用）计的成本，为正即盈利。"""
    quantity: dict[str, int] = {}
    cost: dict[str, float] = {}
    wins = trades = 0
    for f in fills.to_dict("records"):
        symbol, qty, amount, fee = f["symbol"], int(
            f["quantity"]), f["quantity"] * f["price"], f["fee"]
        held = quantity.get(symbol, 0)
        if f["side"] == Side.BUY.value:
            quantity[symbol] = held + qty
            cost[symbol] = cost.get(symbol, 0.0) + amount + fee
        else:
            sold_cost = cost[symbol] * qty / held
            quantity[symbol] = held - qty
            cost[symbol] -= sold_cost
            trades += 1
            wins += amount - fee - sold_cost > 0
    return wins, trades
