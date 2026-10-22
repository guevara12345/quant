from datetime import date

from quant.engine import BacktestConfig, run_backtest
from quant.strategies.ma_cross import MaCross

from .conftest import make_bar, make_feed, stock_costs

SYMBOL = "sh.600519"
NO_COSTS = BacktestConfig(initial_cash=100_000,
                          cash_rate=0.0, stock=stock_costs())


def test_golden_cross_buys_and_death_cross_sells_on_next_day_open():
    # MA2 / MA3（按收盘价）：
    # 01-02..01-04 均为 10，均线相等，无信号
    # 01-05 收 9：MA2 9.5 < MA3 9.67（下穿，但空仓，不卖）
    # 01-08 收 12：MA2 10.5 > MA3 10.33，金叉 → 01-09 开盘 11 买入 9,000 股（100,000 / 11 = 9,090.9，按整手取整）
    # 01-10 收 8：MA2 10 < MA3 10.67，死叉 → 01-11 开盘 8.5 卖出
    feed = make_feed(SYMBOL, {
        date(2024, 1, 2): (10, 10),
        date(2024, 1, 3): (10, 10),
        date(2024, 1, 4): (10, 10),
        date(2024, 1, 5): (10, 9),
        date(2024, 1, 8): (9, 12),
        date(2024, 1, 9): (11, 12),
        date(2024, 1, 10): (12, 8),
        date(2024, 1, 11): (8.5, 8.5),
    })
    result = run_backtest(feed, MaCross(fast=2, slow=3), symbol=SYMBOL,
                          start=date(2024, 1, 2), end=date(2024, 1, 11), config=NO_COSTS)

    fills = result.fills[["date", "side",
                          "quantity", "price"]].to_dict("records")
    assert fills == [
        {"date": date(2024, 1, 9), "side": "buy",
         "quantity": 9000, "price": 11.0},
        {"date": date(2024, 1, 11), "side": "sell",
         "quantity": 9000, "price": 8.5},
    ]


def test_ex_rights_gap_does_not_trigger_death_cross_because_signal_uses_adjusted_price():
    # 01-08 金叉 → 01-09 开盘 11 买入 9,000 股。01-10 除权（每 10 股送 10 股，因子 1 → 2）：
    # 不复权收盘 12 → 6，若按不复权价 MA2 9 < MA3 10 会死叉卖出；
    # 后复权价保持 12，MA2 12 = MA3 12，不下穿，继续持有，持股放大到 18,000 股
    feed = make_feed(SYMBOL, {
        date(2024, 1, 2): (10, 10),
        date(2024, 1, 3): (10, 10),
        date(2024, 1, 4): (10, 10),
        date(2024, 1, 5): (10, 9),
        date(2024, 1, 8): (9, 12),
        date(2024, 1, 9): (11, 12),
        date(2024, 1, 10): make_bar(6, 6, adj_factor=2.0),
        date(2024, 1, 11): make_bar(6.1, 6.1, adj_factor=2.0),
    })
    result = run_backtest(feed, MaCross(fast=2, slow=3), symbol=SYMBOL,
                          start=date(2024, 1, 2), end=date(2024, 1, 11), config=NO_COSTS)

    assert list(result.fills["side"]) == ["buy"]
    assert result.daily["positions"].iloc[-1] == {SYMBOL: 18_000}
