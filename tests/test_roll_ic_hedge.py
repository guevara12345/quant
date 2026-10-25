"""滚 IC + put delta 中性（ticket 06）：建仓对冲、阈值再平衡、put 换月、IC 展期时对冲关系保持、收益拆解。

510500 默认按 5.5 与 5.555 逐日交替：已实现波动率恒定（约 18%），平值行权价恒为 5.5。
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from quant.calendars import weekdays_after
from quant.engine import BacktestConfig, run_backtest
from quant.instruments.option import OptionConfig
from quant.metrics import basis_table, buy_and_hold_nav, compute_metrics, delta_table, return_breakdown, split_metrics
from quant.pricing import put_delta, realized_vol
from quant.report import write_report
from quant.strategies.roll_ic import PutHedge, RollIC

from .conftest import future_costs, make_bar, make_future_bar, make_multi_feed

ETF, INDEX = "sh.510500", "sh.000905"
OPTION = OptionConfig()
NO_COSTS = BacktestConfig(initial_cash=3_000_000, cash_rate=0.0, future=future_costs(),
                          option=OptionConfig(fee_per_contract=0.0))


def weekdays(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5]


DAYS = weekdays(date(2024, 1, 2), date(2024, 3, 29))
START = date(2024, 2, 1)
# 交易日历覆盖到 IC2406、4 月 put 的到期日之后（CLI 用工作日延伸）
CALENDAR = DAYS + weekdays_after(DAYS[-1], date(2024, 7, 31))


def hedged_feed(etf: dict[date, float] | None = None, ic: dict[str, float] | None = None,
                ic_on: dict[tuple[str, date], float] | None = None, index_on: dict[date, float] | None = None):
    """etf 覆盖个别日子的 510500 收盘价；IC 各合约、中证 500 每日价格恒定，ic_on、index_on 覆盖个别日子。"""
    closes = {d: 5.5 * (1.01 if i % 2 else 1.0) for i, d in enumerate(DAYS)}
    closes.update(etf or {})
    prices = ic or {"IC2403": 5800, "IC2406": 5700}
    bars = {s: {d: make_future_bar((ic_on or {}).get((s, d), p)) for d in DAYS} for s, p in prices.items()}
    bars[ETF] = {d: make_bar(c) for d, c in closes.items()}
    bars[INDEX] = {d: make_bar((index_on or {}).get(d, 5900)) for d in DAYS}
    return make_multi_feed(bars, calendar=CALENDAR)


def strategy(threshold: float = 0.1) -> RollIC:
    return RollIC(multiplier=200, hedge=PutHedge(option=OPTION, threshold=threshold))


def model_vol(feed, day: date) -> float:
    """ADR-0002：截至当日 20 个日收益率的已实现波动率 × 1.1。"""
    closes = [feed.bar(ETF, d).adj_close for d in feed.trading_days(DAYS[0], day)][-21:]
    return realized_vol(closes) * 1.1


def put_trade_days(result) -> list[date]:
    return sorted(set(result.fills.loc[~result.fills["symbol"].str.startswith("IC"), "date"]))


def ratios(result, feed, threshold: float = 0.1):
    """每日收盘后的净 delta / IC 名义本金。"""
    hedge = PutHedge(option=OPTION, threshold=threshold)
    return delta_table(result, feed, NO_COSTS, hedge).set_index("date")["net_delta_ratio"]


def puts_held(result, day: date) -> dict[str, int]:
    positions = result.daily.set_index("date").loc[day, "positions"]
    return {s: q for s, q in positions.items() if not s.startswith("IC")}


def test_opening_hedge_buys_at_the_money_next_month_puts_leaving_net_delta_within_one_contract():
    feed = hedged_feed()
    result = run_backtest(feed, strategy(), start=START, end=START, config=NO_COSTS)

    # 02-01 次月 = 3 月，平值行权价 5.5，03-27 到期：剩余 55 个自然日
    delta = put_delta(5.5, 5.5, 55 / 365, OPTION.risk_free_rate, OPTION.dividend_yield, model_vol(feed, START))
    per_contract = 10000 * 5.5 * delta
    ic_delta = 2 * 200 * 5800
    held = puts_held(result, START)
    assert list(held) == ["510500P2403M05500"]
    assert abs(ic_delta + held["510500P2403M05500"] * per_contract) <= abs(per_contract)


def test_rebalances_only_on_days_net_delta_exceeds_threshold():
    # 交替行情下净 delta 每日摆动约 11% 的 IC 名义本金：阈值 20% 时不动；02-15 ETF 跌到 5.2，put delta 大幅变负，超过阈值
    feed = hedged_feed(etf={date(2024, 2, 15): 5.2})
    result = run_backtest(feed, strategy(0.2), start=START, end=date(2024, 2, 15), config=NO_COSTS)

    assert put_trade_days(result) == [START, date(2024, 2, 15)]
    after = ratios(result, feed, 0.2)
    assert (after.abs() <= 0.2).all()
    assert abs(after[date(2024, 2, 15)]) < 0.02  # 再平衡后回到 0 附近（误差为一张）


def test_rebalance_keeps_put_quantity_change_only_while_strike_is_still_at_the_money():
    # 每日再平衡；02-15 ETF 收盘 5.52，平值仍是 5.5：只调张数
    feed = hedged_feed(etf={date(2024, 2, 15): 5.52})
    result = run_backtest(feed, strategy(0.0), start=START, end=date(2024, 2, 15), config=NO_COSTS)

    assert set(result.fills.loc[result.fills["date"] == date(2024, 2, 15), "symbol"]) == {"510500P2403M05500"}


def test_rebalance_after_a_large_move_switches_to_the_next_month_at_the_money_put():
    # ETF 涨到 6.6：5.5 的 put 已深度虚值、delta 接近 0，只调张数会买入成千上万张。
    # 再平衡按建仓时的规则重选 put：02-15 的次月仍是 3 月，平掉旧 put、买入 3 月平值 put（行权价 6.5），张数回到百张以内
    feed = hedged_feed(etf={date(2024, 2, 15): 6.6})
    result = run_backtest(feed, strategy(0.1), start=START, end=date(2024, 2, 15), config=NO_COSTS)

    trades = result.fills[result.fills["date"] == date(2024, 2, 15)]
    assert trades[["symbol", "side"]].to_dict("records") == [
        {"symbol": "510500P2403M05500", "side": "sell"},
        {"symbol": "510500P2403M06500", "side": "buy"},
    ]
    assert trades["quantity"].iloc[0] == puts_held(result, date(2024, 2, 14))["510500P2403M05500"]
    assert trades["quantity"].iloc[1] < 100
    assert abs(ratios(result, feed)[date(2024, 2, 15)]) < 0.02


def test_rebalance_in_the_expiry_month_moves_to_the_next_month_put_instead_of_a_short_dated_one():
    # 阈值 50%：交替行情不触发。03-11 距 3 月 put 到期（03-27）还有 12 个交易日，ETF 涨到 6.6 触发再平衡。
    # 次月已是 4 月：买入 4 月平值 put，而不是只剩两周、delta 很小的 3 月 put
    feed = hedged_feed(etf={date(2024, 3, 11): 6.6})
    result = run_backtest(feed, strategy(0.5), start=START, end=date(2024, 3, 11), config=NO_COSTS)

    trades = result.fills[(result.fills["date"] == date(2024, 3, 11)) & ~result.fills["symbol"].str.startswith("IC")]
    assert list(trades["symbol"]) == ["510500P2403M05500", "510500P2404M06500"]
    assert trades["quantity"].iloc[1] < 100
    assert abs(ratios(result, feed)[date(2024, 3, 11)]) < 0.02


def test_zero_threshold_rebalances_every_day():
    feed = hedged_feed()
    end = date(2024, 2, 9)
    result = run_backtest(feed, strategy(0.0), start=START, end=end, config=NO_COSTS)

    assert put_trade_days(result) == feed.trading_days(START, end)
    assert (ratios(result, feed, 0.0).abs() < 0.02).all()


def test_put_switches_to_new_next_month_at_the_money_contract_five_trading_days_before_expiry():
    # 阈值 50%，其间不触发再平衡。3 月 put 03-27 到期，03-20 距到期还剩 5 个交易日（03-21..03-27）→ 换到 4 月平值 put
    feed = hedged_feed(etf={date(2024, 3, 20): 5.3, date(2024, 3, 21): 5.3, date(2024, 3, 22): 5.3})
    result = run_backtest(feed, strategy(0.5), start=START, end=date(2024, 3, 22), config=NO_COSTS)

    assert list(puts_held(result, date(2024, 3, 19))) == ["510500P2403M05500"]
    switch = result.fills[result.fills["date"] == date(2024, 3, 20)]
    assert switch[["symbol", "side"]].to_dict("records") == [
        {"symbol": "510500P2403M05500", "side": "sell"},
        {"symbol": "510500P2404M05250", "side": "buy"},  # ETF 5.3 → 行权价网格向下取 5.25
    ]
    assert switch["quantity"].iloc[0] == puts_held(result, date(2024, 3, 19))["510500P2403M05500"]
    assert list(puts_held(result, date(2024, 3, 22))) == ["510500P2404M05250"]
    assert abs(ratios(result, feed, 0.5)[date(2024, 3, 20)]) < 0.02
    assert result.alerts.empty


def test_ic_roll_day_keeps_the_hedge_and_net_delta_stays_within_threshold():
    # 03-08 IC2403 展期到 IC2406（手数不变，价格 5800 → 5700，IC 名义本金只变 1.7%），put 不动
    feed = hedged_feed()
    result = run_backtest(feed, strategy(0.2), start=START, end=date(2024, 3, 11), config=NO_COSTS)

    roll = result.fills[result.fills["date"] == date(2024, 3, 8)]
    assert roll[["symbol", "side", "quantity"]].to_dict("records") == [
        {"symbol": "IC2403", "side": "sell", "quantity": 2},
        {"symbol": "IC2406", "side": "buy", "quantity": 2},
    ]
    assert puts_held(result, date(2024, 3, 8)) == puts_held(result, date(2024, 3, 7))
    assert (ratios(result, feed, 0.2).abs() <= 0.2).all()


def test_return_breakdown_sums_to_total_pnl_and_separates_discount_convergence_from_index_move():
    # 02-20 起中证 500 由 5900 涨到 6000，IC2403 由 5800 涨到 5950：基差由 −100 收敛到 −50。
    # 2 手 × 200 × 50 = 20,000 归贴水收敛；指数上涨带来的 2 × 200 × 100 = 40,000 与 put 亏损一起归对冲成本
    later = [d for d in DAYS if d >= date(2024, 2, 20)]
    feed = hedged_feed(ic_on={("IC2403", d): 5950 for d in later}, index_on={d: 6000 for d in later})
    config = BacktestConfig(initial_cash=3_000_000, cash_rate=0.015,
                            future=future_costs(open_fee_rate=0.000023), option=OptionConfig())
    result = run_backtest(feed, strategy(0.2), start=START, end=date(2024, 2, 29), config=config)
    b = return_breakdown(result, feed, INDEX, initial_cash=3_000_000, multiplier=200)

    assert b["total_pnl"] == pytest.approx(result.daily["nav"].iloc[-1] - 3_000_000)
    parts = ["discount_convergence", "hedge_cost", "fees", "interest"]
    assert sum(b[k] for k in parts) == pytest.approx(b["total_pnl"])
    assert b["discount_convergence"] == pytest.approx(20_000)
    assert b["ic_index_pnl"] == pytest.approx(40_000)
    assert b["hedge_cost"] == pytest.approx(b["ic_index_pnl"] + b["put_pnl"])
    puts_traded = result.fills.loc[~result.fills["symbol"].str.startswith("IC"), "quantity"].sum()
    assert b["fees"] == pytest.approx(-(2 * 5800 * 200 * 0.000023 + 2.0 * puts_traded))
    assert b["put_pnl"] < 0  # ETF 未跟随指数上涨：put 只有时间价值流失
    assert 0 < b["interest"] < 3_000_000 * 0.015 * 29 / 365


def test_metrics_split_at_a_date_chain_into_whole_period_return():
    feed = hedged_feed(etf={date(2024, 2, 20): 5.3})
    result = run_backtest(feed, strategy(), start=START, end=date(2024, 2, 29), config=NO_COSTS)
    before, after = split_metrics(result, feed, INDEX, date(2024, 2, 15)).items()

    assert before[0] == "2024-02-01 ~ 2024-02-14"
    assert after[0] == "2024-02-15 ~ 2024-02-29"
    whole = compute_metrics(result, feed, INDEX)["total_return"]
    assert (1 + before[1]["total_return"]) * (1 + after[1]["total_return"]) == pytest.approx(1 + whole)
    assert before[1]["trade_count"] + after[1]["trade_count"] == compute_metrics(result, feed, INDEX)["trade_count"]


def test_report_writes_daily_net_delta_and_plots_hedged_unhedged_and_index_curves(tmp_path):
    feed = hedged_feed()
    end = date(2024, 2, 29)
    hedged = run_backtest(feed, strategy(), start=START, end=end, config=NO_COSTS)
    unhedged = run_backtest(feed, RollIC(multiplier=200), start=START, end=end, config=NO_COSTS)
    extra = basis_table(hedged, feed, INDEX).merge(delta_table(hedged, feed, NO_COSTS, PutHedge(OPTION)), on="date")
    write_report(tmp_path, "t", hedged, buy_and_hold_nav(feed, INDEX, list(hedged.daily["date"]), 3_000_000),
                 compute_metrics(hedged, feed, INDEX), extra_daily=extra,
                 comparisons={"纯滚 IC": unhedged.daily.set_index("date")["nav"]})

    daily = pd.read_csv(tmp_path / "daily.csv", encoding="utf-8-sig")
    assert {"net_delta", "net_delta_ratio", "basis"} <= set(daily.columns)
    assert daily["net_delta_ratio"].abs().max() <= 0.1
    assert (tmp_path / "nav.png").exists()
