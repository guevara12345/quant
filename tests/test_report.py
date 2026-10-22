from datetime import date

import pandas as pd

from quant.engine import BacktestConfig, run_backtest
from quant.metrics import buy_and_hold_nav, compute_metrics
from quant.report import format_rejections, write_report
from quant.strategy import Order, Side, Strategy

from .conftest import make_feed, stock_costs

SYMBOL = "sh.600000"


class BuyAtLimitUp(Strategy):
    def universe(self, day):
        return [SYMBOL]

    def on_bar(self, ctx):
        return [Order(SYMBOL, Side.BUY, 100)] if len(ctx.history(SYMBOL)) == 1 else []


def test_report_lists_skipped_orders_with_reasons(tmp_path):
    days = [date(2024, 1, 2), date(2024, 1, 3)]
    feed = make_feed(SYMBOL, {days[0]: (10, 10),
                     days[1]: (11, 11)})  # 第 2 日开盘涨停
    config = BacktestConfig(initial_cash=100_000,
                            cash_rate=0.0, stock=stock_costs())
    result = run_backtest(feed, BuyAtLimitUp(), start=days[0], end=days[1], config=config)
    write_report(tmp_path, "t", result, buy_and_hold_nav(feed, SYMBOL, days, 100_000),
                 compute_metrics(result, feed, SYMBOL))

    rows = pd.read_csv(tmp_path / "rejections.csv",
                       encoding="utf-8-sig").to_dict("records")
    assert rows == [{"date": "2024-01-03", "symbol": SYMBOL,
                     "side": "buy", "quantity": 100, "reason": "涨停"}]
    assert format_rejections(result) == "未成交订单 1 笔：涨停 1"
