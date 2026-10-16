"""命令行入口：backtest <策略名> [参数]。"""

from __future__ import annotations

import argparse
import dataclasses
import logging
from datetime import date
from pathlib import Path
from typing import Any

from quant.data.baostock_feed import load_baostock_feed
from quant.engine import BacktestConfig, run_backtest
from quant.metrics import buy_and_hold_nav, compute_metrics
from quant.report import format_metrics, write_report
from quant.strategies.ma_cross import MaCross

STRATEGIES = ["ma_cross"]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="backtest", description="A 股日频回测")
    parser.add_argument("strategy", choices=STRATEGIES)
    parser.add_argument("--symbol", default="sh.600519")
    parser.add_argument("--start", type=date.fromisoformat,
                        default=date(2016, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat,
                        default=date.today(), help="默认今天（最新）")
    parser.add_argument("--cash", type=float, default=3_000_000.0, help="初始资金")
    parser.add_argument("--fast", type=int, default=5, help="快线周期")
    parser.add_argument("--slow", type=int, default=20, help="慢线周期")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="覆盖配置，如 --set cash_rate=0.02 --set stock.commission_rate=0.0003")
    parser.add_argument("--cache-dir", type=Path, default=Path("data_cache"))
    parser.add_argument("--out", type=Path, default=Path("output"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    config = apply_overrides(BacktestConfig(initial_cash=args.cash), args.set)
    feed = load_baostock_feed(
        [args.symbol], args.start, args.end, args.cache_dir)
    strategy = MaCross(fast=args.fast, slow=args.slow)
    result = run_backtest(feed, strategy, symbol=args.symbol,
                          start=args.start, end=args.end, config=config)
    if result.daily.empty:
        parser.error(f"{args.start} 至 {args.end} 区间内没有交易日")

    metrics = compute_metrics(
        result, feed, args.symbol, risk_free_rate=config.cash_rate)
    benchmark = buy_and_hold_nav(feed, args.symbol, list(
        result.daily["date"]), config.initial_cash)
    out_dir = args.out / \
        f"{args.strategy}_{args.symbol}_{args.start:%Y%m%d}_{args.end:%Y%m%d}"
    title = f"{args.strategy} MA{args.fast}/MA{args.slow} {args.symbol} {args.start} ~ {result.daily['date'].iloc[-1]}"
    write_report(out_dir, title, result, benchmark, metrics)

    print(title)
    print(format_metrics(metrics))
    print(f"\n报告已输出到 {out_dir}（nav.png、fills.csv、daily.csv、metrics.json）")


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
    new = _replace_path(current, path[1:], value) if len(
        path) > 1 else type(current)(value)
    return dataclasses.replace(obj, **{name: new})  # type: ignore[type-var]


if __name__ == "__main__":
    main()
