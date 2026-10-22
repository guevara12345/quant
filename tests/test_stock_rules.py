"""A 股撮合规则（ticket 02）：T+1、整手、涨跌停、停牌、完整费用、滑点、资金检查，未成交订单记录原因。"""

from datetime import date, timedelta

import pytest

from quant.engine import BacktestConfig, run_backtest
from quant.instruments.stock import StockConfig
from quant.strategy import Order, Side, Strategy

from .conftest import make_bar, make_feed, stock_costs

SYMBOL = "sh.600000"
D1, D2, D3, D4 = date(2024, 1, 2), date(
    2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)

NO_COSTS = BacktestConfig(initial_cash=100_000,
                          cash_rate=0.0, stock=stock_costs())


class Script(Strategy):
    """按信号日序号（第 n 个交易日收盘）发出预先写好的订单。"""

    def __init__(self, orders: dict[int, list[Order]]):
        self.orders = orders

    def on_bar(self, ctx):
        return self.orders.get(len(ctx.history), [])


def rejections(result):
    return result.rejections[["date", "side", "reason"]].to_dict("records")


def test_sell_on_buy_day_is_rejected_for_t_plus_1_and_succeeds_next_day():
    feed = make_feed(SYMBOL, {D1: (10, 10), D2: (10, 10), D3: (10, 10)})
    # 第 1 日收盘同时发出买入和卖出：第 2 日开盘买入成交，卖出因 T+1 被拒；第 2 日收盘再发卖出，第 3 日成交
    strategy = Script({
        1: [Order(SYMBOL, Side.BUY, 1000), Order(SYMBOL, Side.SELL)],
        2: [Order(SYMBOL, Side.SELL)],
    })
    result = run_backtest(feed, strategy, symbol=SYMBOL,
                          start=D1, end=D3, config=NO_COSTS)

    assert result.fills[["date", "side", "quantity"]].to_dict("records") == [
        {"date": D2, "side": "buy", "quantity": 1000},
        {"date": D3, "side": "sell", "quantity": 1000},
    ]
    assert rejections(result) == [
        {"date": D2, "side": "sell", "reason": "T+1"}]


def test_buy_quantity_rounds_down_to_whole_lots():
    feed = make_feed(SYMBOL, {D1: (11, 11), D2: (11, 11), D3: (11, 11)})
    # 指定 1,050 股 → 1,000 股；满仓买入 (100,000 − 11,000) / 11 = 8,090.9 → 8,000 股
    strategy = Script(
        {1: [Order(SYMBOL, Side.BUY, 1050)], 2: [Order(SYMBOL, Side.BUY)]})
    result = run_backtest(feed, strategy, symbol=SYMBOL,
                          start=D1, end=D3, config=NO_COSTS)

    assert result.fills["quantity"].tolist() == [1000, 8000]


def test_buy_skipped_at_limit_up_open_and_sell_skipped_at_limit_down_open():
    feed = make_feed(SYMBOL, {
        D1: (10, 10),
        D2: (10, 10),     # 买入 1,000 股成交
        D3: (9.0, 9.5),   # 开盘 = 跌停价 10 × 0.9：卖单跳过
        D4: (10.45, 10),  # 开盘 = 涨停价 9.5 × 1.1：买单跳过
    })
    strategy = Script({
        1: [Order(SYMBOL, Side.BUY, 1000)],
        2: [Order(SYMBOL, Side.SELL)],
        3: [Order(SYMBOL, Side.BUY, 100)],
    })
    result = run_backtest(feed, strategy, symbol=SYMBOL,
                          start=D1, end=D4, config=NO_COSTS)

    assert len(result.fills) == 1
    assert rejections(result) == [
        {"date": D3, "side": "sell", "reason": "跌停"},
        {"date": D4, "side": "buy", "reason": "涨停"},
    ]


@pytest.mark.parametrize("symbol, fill_day, is_st, limit_up", [
    ("sh.600000", date(2024, 1, 3), False, 11.0),   # 沪市主板 10%
    ("sz.000001", date(2024, 1, 3), False, 11.0),   # 深市主板 10%
    ("sz.300750", date(2020, 8, 21), False, 11.0),  # 创业板改革前 10%
    ("sz.300750", date(2020, 8, 24), False, 12.0),  # 创业板 2020-08-24 起 20%
    ("sh.688981", date(2024, 1, 3), False, 12.0),   # 科创板 20%
    ("sh.600000", date(2024, 1, 3), True, 10.5),    # 主板 ST 5%
    ("sz.300750", date(2020, 8, 21), True, 10.5),   # 创业板改革前 ST 5%
    ("sz.300750", date(2020, 8, 24), True, 12.0),   # 创业板改革后 ST 与板块相同 20%
    ("sh.688981", date(2024, 1, 3), True, 12.0),    # 科创板 ST 20%
])
def test_price_limit_depends_on_board_st_and_chinext_reform_date(symbol, fill_day, is_st, limit_up):
    # 前收盘 10；开盘恰好在涨停价时买单被跳过，低一分钱时成交
    def run(open_price):
        signal_day = fill_day - timedelta(days=1)
        feed = make_feed(symbol, {signal_day: make_bar(
            10, is_st=is_st), fill_day: make_bar(open_price, is_st=is_st)})
        return run_backtest(feed, Script({1: [Order(symbol, Side.BUY, 100)]}), symbol=symbol,
                            start=signal_day, end=fill_day, config=NO_COSTS)

    assert rejections(run(limit_up)) == [
        {"date": fill_day, "side": "buy", "reason": "涨停"}]
    assert len(run(round(limit_up - 0.01, 2)).fills) == 1


def test_orders_do_not_fill_on_suspended_day():
    feed = make_feed(SYMBOL, {D1: (10, 10), D2: make_bar(
        10, suspended=True), D3: (10, 10)})
    strategy = Script({1: [Order(SYMBOL, Side.BUY, 1000)]})
    result = run_backtest(feed, strategy, symbol=SYMBOL,
                          start=D1, end=D3, config=NO_COSTS)

    assert result.fills.empty
    assert rejections(result) == [{"date": D2, "side": "buy", "reason": "停牌"}]


@pytest.mark.parametrize("quantity, buy_fee, sell_fee", [
    # 小额：佣金按最低 5 元；买入 5 + 过户费 0.01，卖出 5 + 印花税 0.5 + 过户费 0.01
    (100, 5.01, 5.51),
    # 大额：成交额 100,000，佣金 25 + 过户费 1，卖出另加印花税 50
    (10_000, 26.0, 76.0),
])
def test_default_fees_with_minimum_commission_and_stamp_duty_on_sells_only(quantity, buy_fee, sell_fee):
    feed = make_feed(SYMBOL, {D1: (10, 10), D2: (10, 10), D3: (10, 10)})
    strategy = Script(
        {1: [Order(SYMBOL, Side.BUY, quantity)], 2: [Order(SYMBOL, Side.SELL)]})
    config = BacktestConfig(initial_cash=1_000_000,
                            cash_rate=0.0, stock=StockConfig(slippage=0.0))
    result = run_backtest(feed, strategy, symbol=SYMBOL,
                          start=D1, end=D3, config=config)

    assert result.fills["fee"].tolist() == pytest.approx([buy_fee, sell_fee])
    assert result.daily["cash"].iloc[-1] == pytest.approx(
        1_000_000 - buy_fee - sell_fee)


def test_slippage_is_adverse_for_both_buys_and_sells():
    feed = make_feed(SYMBOL, {D1: (10, 10), D2: (10, 10), D3: (10, 10)})
    strategy = Script({1: [Order(SYMBOL, Side.BUY, 1000)],
                      2: [Order(SYMBOL, Side.SELL)]})
    config = BacktestConfig(initial_cash=100_000,
                            cash_rate=0.0, stock=stock_costs(slippage=0.001))
    result = run_backtest(feed, strategy, symbol=SYMBOL,
                          start=D1, end=D3, config=config)

    # 开盘都是 10：买入成交于 10.01，卖出成交于 9.99，一买一卖亏掉 1,000 × 0.02
    assert result.fills["price"].tolist() == pytest.approx([10.01, 9.99])
    assert result.daily["cash"].iloc[-1] == pytest.approx(100_000 - 20)


def test_buy_beyond_cash_is_rejected_and_cash_never_goes_negative():
    feed = make_feed(SYMBOL, {D1: (10, 10), D2: (
        10, 10), D3: (10, 10), D4: (10, 10)})
    strategy = Script({
        # 要 200,000，只有 100,000：整单拒绝，不部分成交
        1: [Order(SYMBOL, Side.BUY, 20_000)],
        # 取整到 9,900 股，99,000 + 佣金 24.75 够付
        2: [Order(SYMBOL, Side.BUY, 9_950)],
        3: [Order(SYMBOL, Side.BUY)],          # 剩 975.25，买不起 100 股：满仓买入也被拒
    })
    config = BacktestConfig(initial_cash=100_000, cash_rate=0.0,
                            stock=stock_costs(commission_rate=0.00025, min_commission=5.0))
    result = run_backtest(feed, strategy, symbol=SYMBOL,
                          start=D1, end=D4, config=config)

    assert result.fills["quantity"].tolist() == [9900]
    assert rejections(result) == [
        {"date": D2, "side": "buy", "reason": "资金不足"},
        {"date": D4, "side": "buy", "reason": "资金不足"},
    ]
    assert (result.daily["cash"] >= 0).all()


def test_every_unfilled_order_has_a_reason_including_odd_lot_and_no_position():
    feed = make_feed(SYMBOL, {D1: (10, 10), D2: (10, 10)})
    strategy = Script(
        {1: [Order(SYMBOL, Side.BUY, 50), Order(SYMBOL, Side.SELL)]})
    result = run_backtest(feed, strategy, symbol=SYMBOL,
                          start=D1, end=D2, config=NO_COSTS)

    assert result.fills.empty
    assert rejections(result) == [
        {"date": D2, "side": "buy", "reason": "不足整手"},
        {"date": D2, "side": "sell", "reason": "无持仓"},
    ]
