"""绩效指标与买入持有基准。"""

from __future__ import annotations

import math
from datetime import date
from functools import partial

import pandas as pd

from quant.data.feed import Bar, DataFeed
from quant.engine import BacktestConfig, BacktestResult
from quant.instruments.future import expiry_of, is_future
from quant.instruments.option import OptionRules, is_option
from quant.strategies.roll_ic import PutHedge, net_delta
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


def delta_table(result: BacktestResult, feed: DataFeed, config: BacktestConfig, hedge: PutHedge) -> pd.DataFrame:
    """每日收盘后实际持仓的组合净 delta（元）及其占 IC 名义本金的比例（未持有期货的日子为 NaN）。

    put 按当日模型价反推波动率算 delta，与策略再平衡时的算法一致。
    """
    options = OptionRules(config.option, feed)
    rows = []
    for day, positions in zip(result.daily["date"], result.daily["positions"]):
        delta = net_delta(positions, day, partial(_quote, options, feed, day),
                          config.future.multiplier, hedge, feed.trading_days)
        notional = sum(q * config.future.multiplier * bar.close for s, q in positions.items()
                       if is_future(s) and (bar := feed.bar(s, day)) is not None)
        rows.append({"date": day, "net_delta": delta,
                     "net_delta_ratio": delta / notional if notional else float("nan")})
    return pd.DataFrame(rows, columns=["date", "net_delta", "net_delta_ratio"])


def return_breakdown(result: BacktestResult, feed: DataFeed, index_symbol: str, initial_cash: float,
                     multiplier: int) -> dict[str, float]:
    """滚 IC（+ put）的收益拆解，假设组合只含期货与期权。各项都是对总盈亏的贡献（元），亏损为负：

    - discount_convergence 贴水收敛：IC 盈亏（不含费用）减去其中由中证 500 涨跌带来的部分，即基差变化的贡献；
      期货滑点也落在这一项
    - hedge_cost 指数涨跌与 put = put_pnl（put 盈亏，不含费用，期末持仓按模型价计）+ ic_index_pnl（IC 随指数涨跌的盈亏）；
      delta 中性时两者大体相抵，剩下的是 put 的时间价值流失与对冲误差。不对冲时就是 IC 的指数敞口
    - fees 交易费用（期货手续费与期权每张费用），interest 现金利息

    discount_convergence + hedge_cost + fees + interest = total_pnl（期末净值 − 初始资金）。
    ic_index_pnl 按前一交易日收盘的 IC 手数 × 乘数 × 当日指数收盘价变化逐日累加。
    """
    daily, fills = result.daily, result.fills
    total = daily["nav"].iloc[-1] - initial_cash
    fees = -fills["fee"].sum()
    interest = daily["interest"].sum()
    options = fills[fills["symbol"].map(is_option)]
    direction = options["side"].map({Side.SELL.value: 1, Side.BUY.value: -1})
    signed = options["quantity"] * options["price"] * options["multiplier"] * direction
    put_pnl = signed.sum() + daily["market_value"].iloc[-1]
    future_pnl = total - put_pnl - fees - interest

    ic_index_pnl = 0.0
    prev_lots, prev_index = 0, None
    for day, positions in zip(daily["date"], daily["positions"]):
        bar = feed.bar(index_symbol, day)
        if bar is not None:
            if prev_index is not None:
                ic_index_pnl += prev_lots * multiplier * (bar.close - prev_index)
            prev_index = bar.close
        prev_lots = sum(q for s, q in positions.items() if is_future(s))
    return {
        "total_pnl": total,
        "discount_convergence": future_pnl - ic_index_pnl,
        "hedge_cost": put_pnl + ic_index_pnl,
        "fees": fees,
        "interest": interest,
        "put_pnl": put_pnl,
        "ic_index_pnl": ic_index_pnl,
    }


def _quote(options: OptionRules, feed: DataFeed, day: date, symbol: str) -> Bar | None:
    return options.quote(symbol, day) if is_option(symbol) else feed.bar(symbol, day)


def compute_metrics(
        result: BacktestResult, feed: DataFeed, symbol: str, risk_free_rate: float = 0.015,
        start: date | None = None, end: date | None = None) -> dict:
    """start、end 给出时只统计该区间（含）：净值与基准以区间前一个交易日的收盘为起点（区间从首日开始时以首日为起点），
    成交额、费用按成交日归属，往返交易按卖出日归属（成本仍按全部历史成交计）。"""
    daily = result.daily.set_index("date")["nav"]
    first = start or daily.index[0]
    last = end or daily.index[-1]
    before = daily[daily.index < first]
    nav = pd.concat([before.iloc[-1:], daily[(daily.index >= first) & (daily.index <= last)]])
    initial = nav.iloc[0]
    benchmark = buy_and_hold_nav(feed, symbol, list(nav.index), initial)
    years = (nav.index[-1] - nav.index[0]).days / 365

    fills = result.fills[(result.fills["date"] >= first) & (result.fills["date"] <= last)]
    traded_amount = (fills["quantity"] * fills["price"]
                     * fills["multiplier"]).sum()
    outcomes = [won for day, won in _round_trips(result.fills) if first <= day <= last]
    wins, trades = sum(outcomes), len(outcomes)

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


def split_metrics(result: BacktestResult, feed: DataFeed, symbol: str, split: date,
                  risk_free_rate: float = 0.015) -> dict[str, dict]:
    """以 split 为界分两段统计指标（split 当日归后一段），键为「首日 ~ 末日」；某一段没有交易日时省略。"""
    days = list(result.daily["date"])
    segments = [[d for d in days if d < split], [d for d in days if d >= split]]
    return {f"{seg[0]} ~ {seg[-1]}": compute_metrics(result, feed, symbol, risk_free_rate, seg[0], seg[-1])
            for seg in segments if seg}


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


def _round_trips(fills: pd.DataFrame) -> list[tuple[date, bool]]:
    """每笔卖出算一次交易：(卖出日, 是否盈利)。卖出所得（扣费）减去按平均成本（含买入费用）计的成本，为正即盈利。"""
    quantity: dict[str, int] = {}
    cost: dict[str, float] = {}
    out: list[tuple[date, bool]] = []
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
            out.append((f["date"], amount - fee - sold_cost > 0))
    return out
