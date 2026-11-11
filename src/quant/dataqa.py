"""股票日线数据质量检查（学习路线阶段 1 验收任务三）。

输入是与数据源无关的规范化表（QualityInputs，由 data/baostock_feed.load_quality_inputs 组装），
检查分两类：

- 行情本身：缺失交易日、非交易日有数据、价格缺失或非正、开高低收不自洽、超出涨跌停、
  零成交量却有价格变动、停牌日有成交。
- 公司行动四方核对：交易所前收盘价的跳变、分红送转记录、复权因子跳变三者的日期须一致；
  除权参考价须等于按分红送转重算的值；原型按复权因子比例推算的除权参考价
  （instruments/stock.ex_rights_ref_price）须与交易所前收盘价一致。

每条异常需在说明文件（CSV：symbol,date,check,explanation）里给出解释，未解释的条目使验收不通过。
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd

from quant.instruments.stock import StockConfig, ex_rights_ref_price, limit_price, limit_ratio

# 检查项按报告顺序排列；同一天的多条异常按此顺序输出
CHECKS: dict[str, str] = {
    "missing_day": "缺失交易日（首条记录之后的交易日没有数据）",
    "extra_day": "非交易日有数据",
    "bad_price": "价格缺失、为 0 或为负",
    "ohlc_order": "开高低收不自洽（最高价低于开盘或收盘价，或最低价高于它们）",
    "limit_breach": "最高价或最低价超出以前收盘价为基准的涨跌停价",
    "zero_volume_move": "未停牌、成交量为 0，价格却有变动",
    "suspended_trade": "停牌日有成交量",
    "preclose_without_action": "前收盘价不等于上一条记录的收盘价，但没有分红送转记录",
    "action_without_preclose": "有分红送转记录，但前收盘价没有跳变",
    "ex_ref_mismatch": "前收盘价与按分红送转重算的除权参考价不符",
    "factor_without_action": "复权因子跳变，但没有分红送转记录",
    "action_without_factor": "有分红送转记录，但复权因子没有跳变",
    "prototype_ex_ref_gap": "原型按复权因子比例推算的除权参考价与前收盘价不符",
}

DAILY_COLUMNS = ["date", "open", "high", "low", "close", "preclose", "volume", "suspended", "is_st"]
PRICE_COLUMNS = ["open", "high", "low", "close"]

_Adder = Callable[[date, str, str], None]  # add(day, check, detail)


@dataclass(frozen=True)
class QualityInputs:
    """一个品种的规范化输入。

    Attributes:
        symbol: 证券代码，如 sh.600519。
        calendar: 检查区间内的交易日。
        daily: 不复权日线，列见 DAILY_COLUMNS；preclose 是交易所公布的前收盘价（除权日为除权参考价）。
        factors: 后复权因子的全部历史记录，列为 date、back_adjust_factor。
        actions: 分红送转，列为 date（除权除息日）、cash（每股税前现金红利）、shares（每股送股加转增股）。
    """

    symbol: str
    calendar: list[date]
    daily: pd.DataFrame
    factors: pd.DataFrame
    actions: pd.DataFrame


@dataclass(frozen=True)
class Anomaly:
    symbol: str
    date: date
    check: str
    detail: str


def check_quality(inputs: QualityInputs, config: StockConfig | None = None) -> list[Anomaly]:
    """运行全部检查，按日期、再按 CHECKS 的顺序返回异常。"""
    config = config or StockConfig()
    found: list[Anomaly] = []

    def add(day: date, check: str, detail: str) -> None:
        found.append(Anomaly(inputs.symbol, day, check, detail))

    daily = inputs.daily.sort_values("date").reset_index(drop=True)
    calendar = set(inputs.calendar)
    if len(daily):
        first = daily["date"].iloc[0]
        have = set(daily["date"])
        for day in sorted(d for d in calendar if d >= first and d not in have):
            add(day, "missing_day", "")
    for day in daily["date"]:
        if day not in calendar:
            add(day, "extra_day", "")

    for _, r in daily.iterrows():
        _check_row(r, inputs.symbol, config, add)

    for rec in _ex_rows(inputs):
        _check_action(rec, add)

    order = {name: i for i, name in enumerate(CHECKS)}
    return sorted(found, key=lambda a: (a.date, order[a.check]))


def _check_row(r: pd.Series, symbol: str, config: StockConfig, add: _Adder) -> None:
    day = r["date"]
    if r["suspended"]:
        if r["volume"] > 0:
            add(day, "suspended_trade", f"成交量 {r['volume']:g}")
        return
    prices = [r[c] for c in PRICE_COLUMNS]
    if any(pd.isna(p) or p <= 0 for p in prices):
        add(day, "bad_price", "开高低收 " + " / ".join(f"{p:g}" for p in prices))
        return
    if r["high"] < max(r["open"], r["close"]) or r["low"] > min(r["open"], r["close"]):
        add(day, "ohlc_order", "开高低收 " + " / ".join(f"{p:g}" for p in prices))
    preclose = r["preclose"]
    if not pd.isna(preclose) and preclose > 0:
        ratio = limit_ratio(config, symbol, day, bool(r["is_st"]))
        up, down = limit_price(preclose, 1 + ratio), limit_price(preclose, 1 - ratio)
        if _cent(r["high"]) > up or _cent(r["low"]) < down:
            add(day, "limit_breach",
                f"前收盘 {preclose:g}，涨跌停 {down:g} ~ {up:g}，最低 {r['low']:g} 最高 {r['high']:g}")
        if r["volume"] == 0 and (r["high"] != r["low"] or _cent(r["close"]) != _cent(preclose)):
            add(day, "zero_volume_move", f"前收盘 {preclose:g}，收盘 {r['close']:g}")


@dataclass(frozen=True)
class _ExRow:
    """一个疑似除权日的核对依据。expected_ref 只在有分红送转记录时有值，prototype_ref 只在复权因子跳变时有值。"""

    date: date
    prev_close: float
    cash: float
    shares: float
    expected_ref: float | None
    preclose: float
    factor_ratio: float
    prototype_ref: float | None

    @property
    def jumped(self) -> bool:
        return _cent(self.preclose) != _cent(self.prev_close)

    @property
    def has_action(self) -> bool:
        return self.expected_ref is not None

    @property
    def factor_jump(self) -> bool:
        return self.prototype_ref is not None


def _ex_rows(inputs: QualityInputs) -> list[_ExRow]:
    """疑似除权日：前收盘价跳变、有分红送转记录或复权因子跳变的交易日（不含首条记录）。"""
    daily = inputs.daily.sort_values("date").reset_index(drop=True)
    factor = _factor_series(inputs.factors)
    actions = {r["date"]: (float(r["cash"]), float(r["shares"])) for _, r in inputs.actions.iterrows()}
    rows = []
    for i in range(1, len(daily)):
        day, prev = daily["date"].iloc[i], daily["date"].iloc[i - 1]
        prev_close, preclose = float(daily["close"].iloc[i - 1]), float(daily["preclose"].iloc[i])
        ratio = _factor_on(factor, day) / _factor_on(factor, prev)
        factor_jump = abs(ratio - 1) > 1e-9
        action = actions.get(day)
        if _cent(preclose) == _cent(prev_close) and not factor_jump and action is None:
            continue
        cash, shares = action or (0.0, 0.0)
        expected = None if action is None else _cent(
            (Decimal(str(prev_close)) - Decimal(str(cash))) / (1 + Decimal(str(shares))))
        rows.append(_ExRow(day, prev_close, cash, shares, expected, preclose, ratio,
                           ex_rights_ref_price(prev_close, ratio) if factor_jump else None))
    return rows


def _check_action(rec: _ExRow, add: _Adder) -> None:
    day, preclose = rec.date, rec.preclose
    if rec.jumped and not rec.has_action:
        add(day, "preclose_without_action", f"上一收盘 {rec.prev_close:g}，前收盘 {preclose:g}")
    if rec.has_action and not rec.jumped:
        add(day, "action_without_preclose", f"每股派 {rec.cash:g}、送转 {rec.shares:g}，前收盘未变")
    if rec.has_action and rec.jumped and rec.expected_ref != _cent(preclose):
        add(day, "ex_ref_mismatch", f"重算 {rec.expected_ref:g}，前收盘 {preclose:g}")
    if rec.factor_jump and not rec.has_action:
        add(day, "factor_without_action", f"因子比例 {rec.factor_ratio:.6f}")
    if rec.has_action and not rec.factor_jump:
        add(day, "action_without_factor", f"每股派 {rec.cash:g}、送转 {rec.shares:g}")
    if rec.factor_jump and rec.prototype_ref != _cent(preclose):
        add(day, "prototype_ex_ref_gap", f"原型 {rec.prototype_ref:g}，前收盘 {preclose:g}")


def action_table(inputs: QualityInputs) -> pd.DataFrame:
    """逐个疑似除权日列出核对依据：上一收盘、分红送转、重算参考价、交易所前收盘、因子比例、原型参考价。"""
    columns = ["date", "prev_close", "cash", "shares", "expected_ref", "preclose", "factor_ratio", "prototype_ref"]
    return pd.DataFrame([asdict(r) for r in _ex_rows(inputs)], columns=columns)


def load_notes(path: Path) -> dict[tuple[str, date, str], str]:
    """读取异常说明：(symbol, date, check) → explanation。文件不存在时视为没有说明。"""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {(r["symbol"], date.fromisoformat(r["date"]), r["check"]): r["explanation"].strip()
                for r in csv.DictReader(f)}


def annotate(anomalies: list[Anomaly], notes: dict[tuple[str, date, str], str]) -> pd.DataFrame:
    """异常表加上检查项说明和解释；explanation 为空串即未解释。"""
    return pd.DataFrame([{
        "symbol": a.symbol, "date": a.date, "check": a.check, "description": CHECKS[a.check],
        "detail": a.detail, "explanation": notes.get((a.symbol, a.date, a.check), ""),
    } for a in anomalies], columns=["symbol", "date", "check", "description", "detail", "explanation"])


def format_summary(symbol: str, daily: pd.DataFrame, table: pd.DataFrame, actions: pd.DataFrame) -> str:
    """终端与 summary.md 共用的摘要：覆盖范围、各检查项的异常数与未解释数、除权日核对结果。"""
    lines = [f"# 数据质量报告：{symbol}", ""]
    if len(daily):
        lines.append(f"- 记录 {len(daily)} 条，{daily['date'].min()} 至 {daily['date'].max()}，"
                     f"停牌 {int(daily['suspended'].sum())} 日")
    lines.append(f"- 疑似除权日 {len(actions)} 个，明细见 actions.csv")
    unexplained = int((table["explanation"] == "").sum())
    lines += [f"- 异常 {len(table)} 条，未解释 {unexplained} 条", "", "| 检查项 | 说明 | 异常 | 未解释 |",
              "|---|---|---|---|"]
    for name, desc in CHECKS.items():
        hits = table[table["check"] == name]
        lines.append(f"| {name} | {desc} | {len(hits)} | {int((hits['explanation'] == '').sum())} |")
    lines += ["", "结论：" + ("通过（每条异常都有解释）" if unexplained == 0 else "未通过（存在未解释的异常，见 anomalies.csv）")]
    return "\n".join(lines)


def _factor_series(factors: pd.DataFrame) -> pd.Series:
    return pd.Series(factors["back_adjust_factor"].astype(float).to_numpy(), index=factors["date"]).sort_index()


def _factor_on(factor: pd.Series, day: date) -> float:
    earlier = factor[factor.index <= day]
    return float(earlier.iloc[-1]) if len(earlier) else 1.0


def _cent(x: float | Decimal) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
