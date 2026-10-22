"""绩效指标与买入持有基准。"""

from __future__ import annotations

import math

import pandas as pd

from quant.data.feed import DataFeed
from quant.engine import BacktestResult
from quant.instruments.future import expiry_of, is_future
from quant.strategy import Side

TRADING_DAYS_PER_YEAR = 252


def buy_and_hold_nav(
        feed: DataFeed, symbol: str, days: list, initial_cash: float) -> pd.Series:
    """基准：首日按后复权收盘价全额持有到最后一日，不计费用。"""
    prices = pd.Series({d: bar.adj_close for d in days if (
        bar := feed.bar(symbol, d)) is not None})
    return initial_cash * prices / prices.iloc[0]


def basis_table(result: BacktestResult, feed: DataFeed, index_symbol: str) -> pd.DataFrame:
    """每日收盘后所持期货合约的基差（期货收盘价 − 指数收盘价）与年化贴水率。

    年化贴水率 = −基差 / 指数点位 × 365 / 距到期自然日数；贴水时为正。未持有期货或缺行情的日子为 NaN。
    """
    rows = []
    for day, positions in zip(result.daily["date"], result.daily["positions"]):
        contract = next((s for s in positions if is_future(s)), None)
        future = feed.bar(contract, day) if contract else None
        index = feed.bar(index_symbol, day)
        basis = discount = float("nan")
        if contract and future is not None and index is not None:
            basis = future.close - index.close
            days_left = (expiry_of(contract, feed.trading_days) - day).days
            if days_left > 0:
                discount = -basis / index.close * 365 / days_left
        rows.append({"date": day, "contract": contract,
                     "basis": basis, "annualized_discount": discount})
    return pd.DataFrame(rows, columns=["date", "contract", "basis", "annualized_discount"])


def compute_metrics(
        result: BacktestResult, feed: DataFeed, symbol: str, risk_free_rate: float = 0.015) -> dict:
    nav = result.daily.set_index("date")["nav"]
    initial = nav.iloc[0]
    benchmark = buy_and_hold_nav(feed, symbol, list(nav.index), initial)
    years = (nav.index[-1] - nav.index[0]).days / 365

    fills = result.fills
    traded_amount = (fills["quantity"] * fills["price"]
                     * fills["multiplier"]).sum()
    wins, trades = _round_trip_wins(fills)

    metrics = {
        "total_return": nav.iloc[-1] / initial - 1,
        "annual_return": _annualize(nav, years),
        "max_drawdown": _max_drawdown(nav),
        "sharpe": _sharpe(nav, risk_free_rate),
        "win_rate": wins / trades if trades else float("nan"),
        "trade_count": trades,
        # 年化换手率：单边成交额（衍生品为名义本金）/ 平均净值 / 年数
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
            f["quantity"]), f["quantity"] * f["price"] * f["multiplier"], f["fee"]
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
