"""报告输出：指标文本、净值图（策略 vs 基准，可加对照曲线与净 delta）、成交表、未成交订单表、警告表和每日状态表。"""

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


BREAKDOWN_LABELS = {
    "discount_convergence": "贴水收敛",
    "hedge_cost": "指数涨跌与 put",
    "put_pnl": "  其中 put 盈亏",
    "ic_index_pnl": "  其中 IC 随指数涨跌",
    "fees": "交易费用",
    "interest": "现金利息",
    "total_pnl": "总盈亏",
}


def format_breakdown(breakdowns: dict[str, dict]) -> str:
    """收益拆解，每个组合一列（元）；「指数涨跌与 put」= put 盈亏 + IC 随指数涨跌，对冲时即 put 的对冲成本。"""
    names = list(breakdowns)
    lines = ["收益拆解（元）" + "".join(f"{n:>16}" for n in names)]
    for key, label in BREAKDOWN_LABELS.items():
        lines.append(f"{label:<12}" + "".join(f"{b[key]:>16,.0f}" for b in breakdowns.values()))
    return "\n".join(lines)


def format_segments(segments: dict[str, dict]) -> str:
    return "\n\n".join(f"[{label}]\n{format_metrics(m)}" for label, m in segments.items())


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
    comparisons: dict[str, pd.Series] | None = None,
) -> None:
    """extra_daily：按 date 并入 daily.csv 的附加列（如基差、年化贴水率、净 delta），含 net_delta_ratio 时
    净值图下方加一栏净 delta / IC 名义本金；labels：净值图中策略与基准的图例；comparisons：图例 → 净值，额外的对照曲线。"""
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

    with_delta = extra_daily is not None and "net_delta_ratio" in extra_daily
    fig, axes = plt.subplots(2 if with_delta else 1, 1, figsize=(12, 8 if with_delta else 6), sharex=True,
                             squeeze=False, gridspec_kw={"height_ratios": [3, 1]} if with_delta else None)
    ax = axes[0][0]
    nav = result.daily.set_index("date")["nav"]
    curves = {labels[0]: nav, **(comparisons or {}), labels[1]: benchmark}
    for i, (label, curve) in enumerate(curves.items()):
        ax.plot(pd.to_datetime(curve.index), curve / curve.iloc[0], label=label, alpha=1.0 if i == 0 else 0.7)
    ax.set_title(title)
    ax.set_ylabel("净值")
    ax.grid(alpha=0.3)
    ax.legend()
    if with_delta:
        assert extra_daily is not None
        bottom = axes[1][0]
        bottom.plot(pd.to_datetime(extra_daily["date"]), extra_daily["net_delta_ratio"], linewidth=0.8)
        bottom.axhline(0, color="gray", linewidth=0.5)
        bottom.set_ylabel("净 delta / IC 名义本金")
        bottom.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "nav.png", dpi=120)
    plt.close(fig)
