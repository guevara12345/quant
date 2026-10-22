"""联网冒烟测试：验证 baostock 适配器能拉到数据并组装成 DataFeed。默认跳过，用 pytest --run-network 启用。"""

from datetime import date

import pytest

from quant.data.baostock_feed import load_baostock_feed
from quant.engine import BacktestConfig, run_backtest
from quant.strategy import Order, Side, Strategy

from .conftest import stock_costs


@pytest.mark.network
def test_baostock_feed_loads_moutai(tmp_path):
    feed = load_baostock_feed(["sh.600519"], date(2024, 6, 1), date(2024, 6, 30), tmp_path)
    days = feed.trading_days(date(2024, 6, 1), date(2024, 6, 30))
    assert date(2024, 6, 10) not in days  # 端午节
    assert len(days) == 19
    bar = feed.bar("sh.600519", days[0])
    assert bar is not None and bar.close > 0
    assert bar.adj_close > bar.close  # 后复权价高于不复权价
    assert bar.adj_factor > 1
    assert not bar.suspended and not bar.is_st


class BuyAndHold(Strategy):
    def __init__(self, symbol: str):
        self.symbol = symbol

    def universe(self, day):
        return [self.symbol]

    def on_bar(self, ctx):
        return [Order(self.symbol, Side.BUY)] if len(ctx.history(self.symbol)) == 1 else []


@pytest.mark.network
def test_moutai_buy_and_hold_tracks_backward_adjusted_return_across_ex_rights_days(tmp_path):
    # 2016–2024 年茅台每年分红除权；持股随复权因子放大后，持仓市值涨幅应与后复权价涨幅一致。
    # 不做除权调整时，持股数不变，市值涨幅会少掉这些年的累计分红（约 16%）
    symbol, start, end = "sh.600519", date(2016, 1, 1), date(2024, 12, 31)
    feed = load_baostock_feed([symbol], start, end, tmp_path)
    config = BacktestConfig(initial_cash=3_000_000, cash_rate=0.0, stock=stock_costs())
    result = run_backtest(feed, BuyAndHold(symbol), start=start, end=end, config=config)

    daily = result.daily.set_index("date")
    bought = result.fills.iloc[0]["date"]
    held = daily.loc[bought:]
    adj_close = [feed.bar(symbol, d).adj_close for d in (bought, held.index[-1])]
    # 比较市值而非净值：买入后剩下的不足整手的现金不随股价增长；剩余偏差来自除权时的整股取整
    assert held["market_value"].iloc[-1] / held["market_value"].iloc[0] == pytest.approx(
        adj_close[1] / adj_close[0], rel=0.001)
