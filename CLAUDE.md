# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A 股量化研究原型：本地运行、命令行驱动的**日频回测**（股票、中金所 IC 期货、上交所 ETF 期权）。需求在 `.scratch/a-share-quant-prototype/spec.md`，按 ticket 01–06 演进；目前只完成 ticket 01（骨架 + 双均线）。

## 常用命令

```bash
sh setup.sh                      # 或 ./setup.ps1：建 .venv 并 pip install -r requirements.txt（含 -e .）
.venv/Scripts/python -m pytest   # 全部测试；联网冒烟测试（@pytest.mark.network）默认跳过
.venv/Scripts/python -m pytest --run-network            # 连同 baostock 联网测试一起跑
.venv/Scripts/python -m pytest tests/test_engine.py::<test_name>   # 单个测试
.venv/Scripts/python -m mypy src tests                  # 类型检查（目前零报错，保持住）
.venv/Scripts/backtest ma_cross --symbol sh.600519 --start 2016-01-01 --set stock.commission_rate=0.0003
```

`backtest` 结果写到 `output/<策略>_<代码>_<起>_<止>/`（nav.png、fills.csv、daily.csv、metrics.json）；行情缓存在 `data_cache/`（Parquet，缓存命中时不联网）。两者都已 gitignore。`--set a.b=value` 按路径覆盖嵌套的 frozen dataclass 配置（`BacktestConfig`）。

## 架构要点

数据流：`cli.main` → `load_baostock_feed`（经 `ParquetCache` 只补拉缺失区间）→ `InMemoryDataFeed` → `engine.run_backtest` 逐交易日推进 → `BacktestResult(daily, fills)` → `metrics.compute_metrics` / `report.write_report`。

- **一个引擎、一个账户**（ADR-0001）：所有品种共用 `Portfolio` 现金账户，品种特有逻辑放在 `instruments/<品种>.py` 的规则对象里，引擎不写品种逻辑。目前引擎仍是单标的、规则写死为 `StockRules`，ticket 04 要改成多标的 + 按品种分派。
- **不引入未来函数**：策略在信号日收盘看到截至当日的 `Context`；股票订单进 `pending`，次一交易日开盘撮合；衍生品当日收盘价/模型价成交（ADR-0003）。期权无真实市场价，用 BS 模型价（ADR-0002）。
- **唯一测试接缝是 `DataFeed`**：测试用 `tests/conftest.py` 的 `make_bar` / `make_feed` 手工构造 bar，只断言回测结果，不 mock 内部。
- **外部依赖集中**：数据源的字段名只出现在对应适配器（如 `data/baostock_feed.py`）。
- **参数全部可配置**：费率、滑点、利率等放在 frozen dataclass 配置里，不写死在逻辑中。
- 估值用不复权收盘价，基准（买入持有）用后复权价。

已知的现存缺口（未成交静默丢弃、买单不查资金、无除权调整等）见 `docs/code-walkthrough.md` 第 14 节；02–06 的目标形态、每日事件顺序、各品种规则见 `docs/design.md`。改代码后同步更新这两份文档中受影响的部分（导读里的源码行号也一样）。

## 文档与约定

- 文档、注释、docstring、测试说明均用中文。
- 领域术语以 `GLOSSARY.md` 为准，避开其中标注 _Avoid_ 的同义词（如说「展期」不说「换月」）。
- 与 `docs/adr/` 中决策冲突时要明确指出，不要静默推翻。

## Agent skills

### Issue tracker

Issues 以本地 Markdown 文件形式存放在 `.scratch/<feature>/` 下。See `docs/agents/issue-tracker.md`.

### Triage labels

使用默认的五个 triage 标签（`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`）。See `docs/agents/triage-labels.md`.

### Domain docs

Single-context：根目录一个 `GLOSSARY.md` + `docs/adr/`。See `docs/agents/domain.md`.
