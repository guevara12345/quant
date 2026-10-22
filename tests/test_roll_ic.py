"""纯滚 IC 策略（ticket 04）：按 1 倍名义本金建仓当季合约，到期前 N 个交易日展期到下一个季月合约。"""

from datetime import date, timedelta

from quant.calendars import weekdays_after
import pandas as pd
import pytest

from quant.engine import BacktestConfig, run_backtest
from quant.metrics import basis_table, buy_and_hold_nav, compute_metrics
from quant.report import write_report
from quant.strategies.roll_ic import RollIC

from .conftest import future_costs, make_bar, make_future_bar, make_multi_feed

INDEX = "sh.000905"
NO_COSTS = BacktestConfig(initial_cash=3_000_000,
                          cash_rate=0.0, future=future_costs())

# 2024-03-01（周五）至 03-22 的工作日，没有节假日；IC2403 于 03-15（第三个周五）到期
DAYS = [date(2024, 3, 1) + timedelta(days=i) for i in range(22)
        if (date(2024, 3, 1) + timedelta(days=i)).weekday() < 5]


# 交易日历要覆盖持仓合约的到期日（CLI 用工作日把日历延伸到未来）
CALENDAR = DAYS + weekdays_after(DAYS[-1], date(2024, 7, 31))


def feed_with(prices: dict[str, float], index: float | None = None):
    """各合约每日价格恒定；给出 index 时同时提供中证 500 指数。"""
    bars = {s: {d: make_future_bar(p) for d in DAYS} for s, p in prices.items()}
    if index is not None:
        bars[INDEX] = {d: make_bar(index) for d in DAYS}
    return make_multi_feed(bars, calendar=CALENDAR)


def test_opens_current_quarter_contract_with_lots_sized_to_one_times_notional():
    # 3,000,000 / (5800 × 200 = 1,160,000) = 2.59 → 2 手
    feed = feed_with({"IC2403": 5800, "IC2406": 5700})
    result = run_backtest(feed, RollIC(multiplier=200), start=DAYS[0],
                          end=DAYS[0], config=NO_COSTS)

    assert result.fills[["symbol", "side", "quantity"]].to_dict("records") == [
        {"symbol": "IC2403", "side": "buy", "quantity": 2}]


def test_rolls_to_next_quarter_contract_five_trading_days_before_expiry_keeping_lots():
    # 03-08 距 03-15 到期还剩 5 个交易日（03-11..03-15）→ 展期。IC2406 按 4000 算可开 3 手，但手数保持 2
    feed = feed_with({"IC2403": 5800, "IC2406": 4000})
    result = run_backtest(feed, RollIC(multiplier=200), start=DAYS[0],
                          end=DAYS[-1], config=NO_COSTS)

    assert result.fills[["date", "symbol", "side", "quantity"]].to_dict("records") == [
        {"date": date(2024, 3, 1), "symbol": "IC2403",
         "side": "buy", "quantity": 2},
        {"date": date(2024, 3, 8), "symbol": "IC2403",
         "side": "sell", "quantity": 2},
        {"date": date(2024, 3, 8), "symbol": "IC2406",
         "side": "buy", "quantity": 2},
    ]
    assert result.alerts.empty
    assert result.daily["positions"].iloc[-1] == {"IC2406": 2}


def test_roll_days_before_expiry_is_configurable():
    feed = feed_with({"IC2403": 5800, "IC2406": 5700})
    result = run_backtest(feed, RollIC(multiplier=200, roll_days=2), start=DAYS[0],
                          end=DAYS[-1], config=NO_COSTS)

    # 03-13 距到期还剩 2 个交易日（03-14、03-15）
    assert sorted(set(result.fills["date"])) == [
        date(2024, 3, 1), date(2024, 3, 13)]


def test_starting_inside_roll_window_opens_next_quarter_contract_directly():
    feed = feed_with({"IC2403": 5800, "IC2406": 5700})
    result = run_backtest(feed, RollIC(multiplier=200), start=date(2024, 3, 11),
                          end=date(2024, 3, 11), config=NO_COSTS)

    assert list(result.fills["symbol"]) == ["IC2406"]


def test_daily_basis_and_annualized_discount_of_held_contract_against_index():
    # 03-01 持有 IC2403 收盘 5800，中证 500 收盘 5900：基差 −100，贴水 100 / 5900，
    # 距 03-15 到期 14 个自然日，年化 = 100 / 5900 × 365 / 14
    feed = feed_with({"IC2403": 5800, "IC2406": 5700}, index=5900)
    result = run_backtest(feed, RollIC(multiplier=200), start=DAYS[0],
                          end=DAYS[-1], config=NO_COSTS)
    table = basis_table(result, feed, INDEX).set_index("date")

    first = table.loc[date(2024, 3, 1)]
    assert (first["contract"], first["basis"]) == ("IC2403", -100)
    assert first["annualized_discount"] == pytest.approx(
        100 / 5900 * 365 / 14)
    # 03-08 展期后看 IC2406：基差 −200，距 06-21 到期 105 个自然日
    rolled = table.loc[date(2024, 3, 8)]
    assert (rolled["contract"], rolled["basis"]) == ("IC2406", -200)
    assert rolled["annualized_discount"] == pytest.approx(
        200 / 5900 * 365 / 105)


def test_report_daily_csv_includes_margin_basis_and_annualized_discount(tmp_path):
    feed = feed_with({"IC2403": 5800, "IC2406": 5700}, index=5900)
    result = run_backtest(feed, RollIC(multiplier=200), start=DAYS[0],
                          end=DAYS[-1], config=NO_COSTS)
    benchmark = buy_and_hold_nav(feed, INDEX, DAYS, 3_000_000)
    write_report(tmp_path, "t", result, benchmark, compute_metrics(result, feed, INDEX),
                 extra_daily=basis_table(result, feed, INDEX), labels=("纯滚 IC", "中证 500"))

    daily = pd.read_csv(tmp_path / "daily.csv", encoding="utf-8-sig")
    first = daily.iloc[0]
    assert first["margin"] == pytest.approx(2 * 5800 * 200 * 0.14)
    assert first["basis"] == -100
    assert first["annualized_discount"] == pytest.approx(
        100 / 5900 * 365 / 14)
    assert (tmp_path / "nav.png").exists()
