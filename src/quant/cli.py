"""命令行入口：backtest <策略名> [参数]。"""

from __future__ import annotations

import argparse
import dataclasses
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from quant.calendars import weekdays_after
from quant.data.akshare_feed import load_etf_bars, load_index_bars
from quant.data.baostock_feed import load_baostock_feed, load_trade_calendar
from quant.data.cffex_feed import load_cffex_bars
from quant.data.feed import InMemoryDataFeed
from quant.engine import BacktestConfig, BacktestResult, run_backtest
from quant.metrics import (basis_table, buy_and_hold_nav, compute_metrics, delta_table, return_breakdown,
                           split_metrics)
from quant.report import (format_alerts, format_breakdown, format_metrics, format_rejections, format_segments,
                          write_report)
from quant.strategies.ma_cross import MaCross
from quant.strategies.roll_ic import PutHedge, RollIC

STRATEGIES = ["ma_cross", "roll_ic"]
CSI500 = "sh.000905"
ETF500 = "sh.510500"
# 上交所 500ETF 期权上市日：此前的 put 是假设品种，指标按此分段
ETF_OPTION_LISTING = date(2022, 9, 19)
MODEL_PRICE_NOTE = "期权价格为模型价（BS，波动率 = 510500 已实现波动率 × 溢价系数，ADR-0002）"
# 日历延伸到回测结束后多少天：覆盖持仓合约（最远为下一个季月合约）的到期日
CALENDAR_LOOKAHEAD_DAYS = 200


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="backtest", description="A 股日频回测")
    parser.add_argument("strategy", choices=STRATEGIES)
    parser.add_argument("--symbol", default="sh.600519", help="ma_cross 的标的")
    parser.add_argument("--start", type=date.fromisoformat,
                        default=date(2016, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat,
                        default=date.today(), help="默认今天（最新）")
    parser.add_argument("--cash", type=float, default=3_000_000.0, help="初始资金")
    parser.add_argument("--fast", type=int, default=5, help="ma_cross 快线周期")
    parser.add_argument("--slow", type=int, default=20, help="ma_cross 慢线周期")
    parser.add_argument("--no-hedge", action="store_true",
                        help="roll_ic 只滚 IC、不用 put 对冲")
    parser.add_argument("--roll-days", type=int, default=5,
                        help="roll_ic 在到期前多少个交易日展期")
    parser.add_argument("--hedge-threshold", type=float, default=PutHedge.threshold,
                        help="roll_ic |净 delta| 超过 IC 名义本金的这个比例时调整 put 张数，0 即每日再平衡")
    parser.add_argument("--put-roll-days", type=int, default=PutHedge.roll_days,
                        help="roll_ic put 到期前多少个交易日换到新的次月合约")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="覆盖配置，如 --set cash_rate=0.02 --set stock.commission_rate=0.0003")
    parser.add_argument("--cache-dir", type=Path, default=Path("data_cache"))
    parser.add_argument("--out", type=Path, default=Path("output"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    config = apply_overrides(BacktestConfig(initial_cash=args.cash), args.set)
    if args.strategy == "ma_cross":
        result, out_dir = run_ma_cross(args, config, parser)
    else:
        result, out_dir = run_roll_ic(args, config, parser)
    print(format_rejections(result))
    print(format_alerts(result))
    print(
        f"\n报告已输出到 {out_dir}（nav.png、fills.csv、rejections.csv、alerts.csv、daily.csv、metrics.json）")


def run_ma_cross(args: argparse.Namespace, config: BacktestConfig,
                 parser: argparse.ArgumentParser) -> tuple[BacktestResult, Path]:
    feed = load_baostock_feed(
        [args.symbol], args.start, args.end, args.cache_dir)
    result = run_backtest(feed, MaCross(args.symbol, fast=args.fast, slow=args.slow),
                          start=args.start, end=args.end, config=config)
    if result.daily.empty:
        parser.error(f"{args.start} 至 {args.end} 区间内没有交易日")
    metrics = compute_metrics(
        result, feed, args.symbol, risk_free_rate=config.cash_rate)
    benchmark = buy_and_hold_nav(feed, args.symbol, list(
        result.daily["date"]), config.initial_cash)
    out_dir = args.out / f"{args.strategy}_{args.symbol}_{args.start:%Y%m%d}_{args.end:%Y%m%d}"
    title = (f"{args.strategy} MA{args.fast}/MA{args.slow} {args.symbol} "
             f"{args.start} ~ {result.daily['date'].iloc[-1]}")
    write_report(out_dir, title, result, benchmark, metrics)
    print(title)
    print(format_metrics(metrics))
    return result, out_dir


def run_roll_ic(args: argparse.Namespace, config: BacktestConfig,
                parser: argparse.ArgumentParser) -> tuple[BacktestResult, Path]:
    """滚 IC：对冲时同时跑纯滚 IC 作对照，报告三条净值曲线、每日净 delta、收益拆解和按期权上市日分段的指标。"""
    hedge = None if args.no_hedge else PutHedge(
        option=config.option, threshold=args.hedge_threshold, roll_days=args.put_roll_days, underlying=ETF500)
    lookback = None if hedge is None else config.option.vol_lookback_days
    feed, end = load_roll_ic_feed(args.start, args.end, args.cache_dir, etf_lookback_days=lookback)
    multiplier = config.future.multiplier
    unhedged = run_backtest(feed, RollIC(roll_days=args.roll_days, multiplier=multiplier),
                            start=args.start, end=end, config=config)
    if unhedged.daily.empty:
        parser.error(f"{args.start} 至 {args.end} 区间内没有交易日")
    days = list(unhedged.daily["date"])
    benchmark = buy_and_hold_nav(feed, CSI500, days, config.initial_cash)
    breakdowns = {"纯滚 IC": return_breakdown(unhedged, feed, CSI500, config.initial_cash, multiplier)}
    period = f"{args.start:%Y%m%d}_{args.end:%Y%m%d}"
    span = f"{args.start} ~ {days[-1]}"
    comparisons = None
    if hedge is None:
        result, extra_daily = unhedged, basis_table(unhedged, feed, CSI500)
        labels = ("纯滚 IC", "中证 500 买入持有")
        out_dir = args.out / f"{args.strategy}_nohedge_{period}"
        title = f"{args.strategy} 纯滚 IC（展期 {args.roll_days} 日）{span}"
    else:
        result = run_backtest(feed, RollIC(roll_days=args.roll_days, multiplier=multiplier, hedge=hedge),
                              start=args.start, end=end, config=config)
        breakdowns = {"本策略": return_breakdown(result, feed, CSI500, config.initial_cash, multiplier),
                      **breakdowns}
        unhedged_nav = unhedged.daily.set_index("date")["nav"]
        extra_daily = basis_table(result, feed, CSI500).merge(
            delta_table(result, feed, config, hedge), on="date")
        extra_daily["unhedged_nav"] = extra_daily["date"].map(unhedged_nav)
        comparisons = {"纯滚 IC": unhedged_nav}
        labels = ("滚 IC + put delta 中性", "中证 500 买入持有")
        out_dir = args.out / f"{args.strategy}_hedge_{period}"
        title = (f"{args.strategy} 滚 IC + put delta 中性（展期 {args.roll_days} 日，"
                 f"阈值 {args.hedge_threshold:.0%}）{span}\n{MODEL_PRICE_NOTE}")

    metrics = compute_metrics(result, feed, CSI500, risk_free_rate=config.cash_rate)
    metrics["breakdown"] = breakdowns
    if hedge is not None:
        metrics["note"] = f"{MODEL_PRICE_NOTE}；500ETF 期权 {ETF_OPTION_LISTING} 上市，此前的 put 是假设品种"
        metrics["segments"] = split_metrics(result, feed, CSI500, ETF_OPTION_LISTING, config.cash_rate)
    write_report(out_dir, title, result, benchmark, metrics,
                 extra_daily=extra_daily, labels=labels, comparisons=comparisons)

    print(title)
    print(format_metrics(metrics))
    if hedge is not None:
        print(f"\n按 500ETF 期权上市日（{ETF_OPTION_LISTING}）分段，上市前的 put 是假设品种：")
        print(format_segments(metrics["segments"]))
    print()
    print(format_breakdown(breakdowns))
    return result, out_dir


def load_roll_ic_feed(start: date, end: date, cache_dir: Path,
                      etf_lookback_days: int | None = None) -> tuple[InMemoryDataFeed, date]:
    """IC 各合约 + 中证 500 + 交易日历，etf_lookback_days 不为 None 时加上 510500。返回 feed 与实际可回测到的最后一个交易日。

    510500 与交易日历从 start 往前多取 etf_lookback_days 个自然日，期权模型价在回测首日就有完整的波动率窗口。
    已公布的交易日历之后用工作日延伸，供策略和到期判断数到期前的交易日（忽略未来节假日）。
    """
    first = start - timedelta(days=etf_lookback_days or 0)
    calendar = load_trade_calendar(first, end, cache_dir)
    if not calendar or calendar[-1] < start:
        raise SystemExit(f"{start} 至 {end} 区间内没有交易日")
    last = calendar[-1]
    bars = load_cffex_bars("IC", start, last, cache_dir)
    bars[CSI500] = load_index_bars(CSI500, start, last, cache_dir)
    if etf_lookback_days is not None:
        bars[ETF500] = load_etf_bars(ETF500, first, last, cache_dir)
    projected = weekdays_after(
        last, last + timedelta(days=CALENDAR_LOOKAHEAD_DAYS))
    return InMemoryDataFeed(calendar=calendar + projected, bars=bars), last


def apply_overrides(config: BacktestConfig, overrides: list[str]) -> BacktestConfig:
    """按 "a.b=value" 覆盖嵌套的 frozen dataclass 配置，值按原字段类型转换。"""
    for item in overrides:
        key, _, value = item.partition("=")
        config = _replace_path(config, key.split("."), value)
    return config


def _replace_path(obj: Any, path: list[str], value: str) -> Any:
    name = path[0]
    if not dataclasses.is_dataclass(obj) or name not in {f.name for f in dataclasses.fields(obj)}:
        raise SystemExit(f"未知配置项：{name}")
    current = getattr(obj, name)
    if len(path) > 1:
        new = _replace_path(current, path[1:], value)
    elif isinstance(current, date):
        new = date.fromisoformat(value)
    else:
        new = type(current)(value)
    return dataclasses.replace(obj, **{name: new})  # type: ignore[type-var]


if __name__ == "__main__":
    main()
