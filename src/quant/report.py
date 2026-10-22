"""报告输出：指标文本、净值图（策略 vs 基准）、成交表、未成交订单表、警告表和每日状态表。"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from quant.engine import BacktestResult  # noqa: E402

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei",
                                   "SimHei", "PingFang SC", "Noto Sans CJK SC", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

METRIC_LABELS = {
    "total_return": ("总收益", "pct"),
    "annual_return": ("年化收益", "pct"),
    "max_drawdown": ("最大回撤", "pct"),
    "sharpe": ("夏普比率", "num"),
    "win_rate": ("胜率", "pct"),
    "trade_count": ("交易次数", "int"),
    "turnover": ("年化换手率", "num"),
    "total_fees": ("总费用", "money"),
    "benchmark_total_return": ("基准总收益", "pct"),
    "benchmark_annual_return": ("基准年化收益", "pct"),
    "excess_annual_return": ("年化超额收益", "pct"),
}


FORMATS = {"pct": ".2%", "num": ".2f", "int": ".0f", "money": ",.2f"}


def format_metrics(metrics: dict) -> str:
    lines = []
    for key, (label, kind) in METRIC_LABELS.items():
        value = metrics[key]
        text = format(value, FORMATS[kind])
        lines.append(f"{label:<8}{text:>14}")
    return "\n".join(lines)


def format_rejections(result: BacktestResult) -> str:
    """未成交订单按原因计数，如「未成交订单 3 笔：涨停 2、停牌 1」。"""
    rejections = result.rejections
    if rejections.empty:
        return "未成交订单 0 笔"
    counts = "、".join(f"{reason} {n}" for reason,
                      n in rejections["reason"].value_counts().items())
    return f"未成交订单 {len(rejections)} 笔：{counts}"


def format_alerts(result: BacktestResult) -> str:
    alerts = result.alerts
    lines = [f"警告 {len(alerts)} 条"]
    lines += [f"  {a['date']} {a['symbol']}：{a['message']}"
              for a in alerts.to_dict("records")]
    return "\n".join(lines)


def write_report(
    out_dir: Path, title: str, result: BacktestResult, benchmark: pd.Series, metrics: dict,
    extra_daily: pd.DataFrame | None = None,
    labels: tuple[str, str] = ("策略", "买入持有"),
) -> None:
    """extra_daily：按 date 并入 daily.csv 的附加列（如基差、年化贴水率）；labels：净值图中策略与基准的图例。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    result.fills.to_csv(out_dir / "fills.csv",
                        index=False, encoding="utf-8-sig")
    result.rejections.to_csv(out_dir / "rejections.csv",
                             index=False, encoding="utf-8-sig")
    result.alerts.to_csv(out_dir / "alerts.csv",
                         index=False, encoding="utf-8-sig")
    daily = result.daily.copy()
    daily["positions"] = daily["positions"].map(
        lambda p: json.dumps(p, ensure_ascii=False))
    daily["benchmark_nav"] = daily["date"].map(benchmark)
    if extra_daily is not None:
        daily = daily.merge(extra_daily, on="date", how="left")
    daily.to_csv(out_dir / "daily.csv", index=False, encoding="utf-8-sig")
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False, default=float),
                                          encoding="utf-8")

    nav = result.daily.set_index("date")["nav"]
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(pd.to_datetime(nav.index), nav / nav.iloc[0], label=labels[0])
    ax.plot(pd.to_datetime(benchmark.index), benchmark /
            benchmark.iloc[0], label=labels[1], alpha=0.7)
    ax.set_title(title)
    ax.set_ylabel("净值")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "nav.png", dpi=120)
    plt.close(fig)
