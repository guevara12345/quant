"""命令行入口：backtest <策略名> [参数]。"""

from __future__ import annotations

import argparse
import dataclasses
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from quant.calendars import weekdays_after
from quant.data.akshare_feed import load_index_bars
from quant.data.baostock_feed import load_baostock_feed, load_trade_calendar
from quant.data.cffex_feed import load_cffex_bars
from quant.data.feed import InMemoryDataFeed
from quant.engine import BacktestConfig, run_backtest
from quant.metrics import basis_table, buy_and_hold_nav, compute_metrics
from quant.report import format_alerts, format_metrics, format_rejections, write_report
from quant.strategies.ma_cross import MaCross
from quant.strategies.roll_ic import RollIC
from quant.strategy import Strategy

STRATEGIES = ["ma_cross", "roll_ic"]
CSI500 = "sh.000905"
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
                        help="roll_ic 只滚 IC、不用 put 对冲（对冲版本见 ticket 06，尚未实现）")
    parser.add_argument("--roll-days", type=int, default=5,
                        help="roll_ic 在到期前多少个交易日展期")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="覆盖配置，如 --set cash_rate=0.02 --set stock.commission_rate=0.0003")
    parser.add_argument("--cache-dir", type=Path, default=Path("data_cache"))
    parser.add_argument("--out", type=Path, default=Path("output"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    config = apply_overrides(BacktestConfig(initial_cash=args.cash), args.set)
    extra_daily = None
    if args.strategy == "ma_cross":
        feed = load_baostock_feed(
            [args.symbol], args.start, args.end, args.cache_dir)
        strategy: Strategy = MaCross(args.symbol, fast=args.fast, slow=args.slow)
        benchmark_symbol, end = args.symbol, args.end
        labels = ("策略", "买入持有")
        out_name = f"{args.strategy}_{args.symbol}_{args.start:%Y%m%d}_{args.end:%Y%m%d}"
        title = f"{args.strategy} MA{args.fast}/MA{args.slow} {args.symbol}"
    else:
        if not args.no_hedge:
            parser.error("put 对冲版本（ticket 06）尚未实现，目前请加 --no-hedge")
        feed, end = load_roll_ic_feed(args.start, args.end, args.cache_dir)
        strategy = RollIC(roll_days=args.roll_days,
                          multiplier=config.future.multiplier)
        benchmark_symbol = CSI500
        labels = ("纯滚 IC", "中证 500 买入持有")
        out_name = f"{args.strategy}_nohedge_{args.start:%Y%m%d}_{args.end:%Y%m%d}"
        title = f"{args.strategy} 纯滚 IC（展期 {args.roll_days} 日）"

    result = run_backtest(feed, strategy,
                          start=args.start, end=end, config=config)
    if result.daily.empty:
        parser.error(f"{args.start} 至 {args.end} 区间内没有交易日")
    if args.strategy == "roll_ic":
        extra_daily = basis_table(result, feed, CSI500)

    metrics = compute_metrics(
        result, feed, benchmark_symbol, risk_free_rate=config.cash_rate)
    benchmark = buy_and_hold_nav(feed, benchmark_symbol, list(
        result.daily["date"]), config.initial_cash)
    out_dir = args.out / out_name
    title = f"{title} {args.start} ~ {result.daily['date'].iloc[-1]}"
    write_report(out_dir, title, result, benchmark, metrics,
                 extra_daily=extra_daily, labels=labels)

    print(title)
    print(format_metrics(metrics))
    print(format_rejections(result))
    print(format_alerts(result))
    print(
        f"\n报告已输出到 {out_dir}（nav.png、fills.csv、rejections.csv、alerts.csv、daily.csv、metrics.json）")


def load_roll_ic_feed(start: date, end: date, cache_dir: Path) -> tuple[InMemoryDataFeed, date]:
    """IC 各合约 + 中证 500 + 交易日历。返回 feed 与实际可回测到的最后一个交易日。

    已公布的交易日历之后用工作日延伸，供策略和到期判断数到期前的交易日（忽略未来节假日）。
    """
    calendar = load_trade_calendar(start, end, cache_dir)
    if not calendar:
        raise SystemExit(f"{start} 至 {end} 区间内没有交易日")
    last = calendar[-1]
    bars = load_cffex_bars("IC", start, last, cache_dir)
    bars[CSI500] = load_index_bars(CSI500, start, last, cache_dir)
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
