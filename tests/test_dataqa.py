"""数据质量检查（阶段 1）：直接用手工构造的规范化行情表测检查函数，不经 DataFeed。

每个用例从一段干净的数据出发，只植入一处异常，断言恰好报出对应的检查项。
"""

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from quant.dataqa import QualityInputs, action_table, annotate, check_quality, load_notes

SYMBOL = "sh.600000"
DAYS = [date(2024, 1, 2) + timedelta(days=i) for i in range(5)]  # 2024-01-02 至 01-06，当作 5 个交易日


def row(day: date, close: float, preclose: float, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "date": day, "open": close, "high": close, "low": close, "close": close,
        "preclose": preclose, "volume": 1000.0, "suspended": False, "is_st": False,
    }
    return {**base, **overrides}


def clean_daily() -> list[dict[str, object]]:
    closes = [10.0, 10.5, 10.2, 10.8, 11.0]
    return [row(d, c, 9.9 if i == 0 else closes[i - 1]) for i, (d, c) in enumerate(zip(DAYS, closes))]


def inputs(daily: list[dict[str, object]] | None = None, factors: list[tuple[date, float]] | None = None,
           actions: list[tuple[date, float, float]] | None = None, calendar: list[date] | None = None) -> QualityInputs:
    return QualityInputs(
        symbol=SYMBOL,
        calendar=DAYS if calendar is None else calendar,
        daily=pd.DataFrame(clean_daily() if daily is None else daily),
        factors=pd.DataFrame(factors or [(date(2000, 1, 1), 1.0)], columns=["date", "back_adjust_factor"]),
        actions=pd.DataFrame(actions or [], columns=["date", "cash", "shares"]),
    )


def checks(qi: QualityInputs) -> list[tuple[date, str]]:
    return [(a.date, a.check) for a in check_quality(qi)]


def test_clean_data_has_no_anomaly() -> None:
    assert checks(inputs()) == []


def test_missing_trading_day() -> None:
    daily = [r for r in clean_daily() if r["date"] != DAYS[2]]
    daily[2]["preclose"] = 10.5  # 与上一条记录的收盘价衔接，只留下缺失这一处异常
    assert checks(inputs(daily)) == [(DAYS[2], "missing_day")]


def test_row_on_non_trading_day() -> None:
    calendar = [d for d in DAYS if d != DAYS[3]]
    assert (DAYS[3], "extra_day") in checks(inputs(calendar=calendar))


def test_non_positive_price() -> None:
    daily = clean_daily()
    daily[1]["low"] = 0.0
    assert (DAYS[1], "bad_price") in checks(inputs(daily))


def test_ohlc_out_of_order() -> None:
    daily = clean_daily()
    daily[1]["high"] = 10.4  # 低于收盘价 10.5
    assert checks(inputs(daily)) == [(DAYS[1], "ohlc_order")]


def test_limit_breach_uses_preclose() -> None:
    daily = clean_daily()
    # 前收盘 10.5 的主板涨停价是 11.55，最高价 11.56 越过一分钱
    daily[2].update(high=11.56)
    assert checks(inputs(daily)) == [(DAYS[2], "limit_breach")]


def test_exactly_at_limit_is_not_breach() -> None:
    daily = clean_daily()
    daily[2].update(high=11.55, close=11.55)
    daily[3]["preclose"] = 11.55
    assert checks(inputs(daily)) == []


def test_st_limit_is_five_percent() -> None:
    daily = clean_daily()
    daily[2].update(high=11.10, is_st=True)  # 前收盘 10.5 的 ST 涨停价为 11.03
    assert checks(inputs(daily)) == [(DAYS[2], "limit_breach")]


def test_zero_volume_with_price_move() -> None:
    daily = clean_daily()
    daily[1]["volume"] = 0.0
    assert checks(inputs(daily)) == [(DAYS[1], "zero_volume_move")]


def test_suspended_day_with_volume() -> None:
    daily = clean_daily()
    daily[1].update(suspended=True, close=10.0, open=10.0, high=10.0, low=10.0)
    daily[2]["preclose"] = 10.0
    assert checks(inputs(daily)) == [(DAYS[1], "suspended_trade")]


def test_suspended_day_is_not_checked_for_limit_or_zero_volume() -> None:
    daily = clean_daily()
    daily[1].update(suspended=True, volume=0.0, close=10.0, open=10.0, high=10.0, low=10.0)
    daily[2]["preclose"] = 10.0
    assert checks(inputs(daily)) == []


def dividend_case(cash: float = 0.5, shares: float = 0.0, preclose: float = 10.0,
                  factor_ratio: float | None = None) -> QualityInputs:
    """DAYS[2] 除息：前一日收盘 10.5，每股派 cash、送转 shares。"""
    daily = clean_daily()
    daily[2]["preclose"] = preclose
    ratio = 10.5 / preclose if factor_ratio is None else factor_ratio
    factors = [(date(2000, 1, 1), 1.0), (DAYS[2], ratio)]
    return inputs(daily, factors=factors, actions=[(DAYS[2], cash, shares)])


def test_consistent_dividend_has_no_anomaly() -> None:
    assert checks(dividend_case()) == []


def test_bonus_share_ex_reference_price() -> None:
    # 每 10 股送 5 股派 1 元：(10.5 − 0.1) / 1.5 = 6.9333…，取整到 6.93
    daily = clean_daily()[:3]
    daily[2].update(preclose=6.93, open=6.9, high=6.9, low=6.9, close=6.9)
    qi = inputs(daily, calendar=DAYS[:3], factors=[(date(2000, 1, 1), 1.0), (DAYS[2], 10.5 / 6.93)],
                actions=[(DAYS[2], 0.1, 0.5)])
    assert checks(qi) == []


def test_preclose_jump_without_action() -> None:
    daily = clean_daily()
    daily[2]["preclose"] = 10.0
    assert checks(inputs(daily)) == [(DAYS[2], "preclose_without_action")]


def test_action_without_preclose_jump() -> None:
    qi = inputs(actions=[(DAYS[2], 0.5, 0.0)], factors=[(date(2000, 1, 1), 1.0), (DAYS[2], 1.05)])
    assert (DAYS[2], "action_without_preclose") in checks(qi)


def test_ex_reference_price_mismatch() -> None:
    # 派 0.5 元应得 10.00，数据给的是 9.98
    qi = dividend_case(preclose=9.98, factor_ratio=1.05)
    assert checks(qi) == [(DAYS[2], "ex_ref_mismatch"), (DAYS[2], "prototype_ex_ref_gap")]


def test_factor_jump_without_action() -> None:
    qi = inputs(factors=[(date(2000, 1, 1), 1.0), (DAYS[2], 1.05)])
    assert checks(qi) == [(DAYS[2], "factor_without_action"), (DAYS[2], "prototype_ex_ref_gap")]


def test_action_without_factor_jump() -> None:
    daily = clean_daily()
    daily[2]["preclose"] = 10.0
    qi = inputs(daily, actions=[(DAYS[2], 0.5, 0.0)])
    assert checks(qi) == [(DAYS[2], "action_without_factor")]


def test_repeated_factor_record_with_same_value_is_not_a_jump() -> None:
    qi = inputs(factors=[(date(2000, 1, 1), 1.0), (DAYS[2], 1.0)])
    assert checks(qi) == []


def test_prototype_ex_ref_gap() -> None:
    # 交易所参考价 10.00 正确，但复权因子比例 1.04 推出 10.5 / 1.04 = 10.10
    assert checks(dividend_case(factor_ratio=1.04)) == [(DAYS[2], "prototype_ex_ref_gap")]


def test_action_table_lists_each_ex_day() -> None:
    table = action_table(dividend_case())
    assert table.to_dict("records") == [{
        "date": DAYS[2], "prev_close": 10.5, "cash": 0.5, "shares": 0.0, "expected_ref": 10.0,
        "preclose": 10.0, "factor_ratio": pytest.approx(1.05), "prototype_ref": 10.0,
    }]


def test_notes_explain_matching_anomalies(tmp_path: Path) -> None:
    notes = tmp_path / "notes.csv"
    notes.write_text("symbol,date,check,explanation\n"
                     f"{SYMBOL},2024-01-04,preclose_without_action,公司公告的特别分红，数据源缺记录\n",
                     encoding="utf-8")
    daily = clean_daily()
    daily[2]["preclose"] = 10.0
    daily[3]["volume"] = 0.0
    table = annotate(check_quality(inputs(daily)), load_notes(notes))
    assert table[["check", "explanation"]].to_dict("records") == [
        {"check": "preclose_without_action", "explanation": "公司公告的特别分红，数据源缺记录"},
        {"check": "zero_volume_move", "explanation": ""},
    ]
