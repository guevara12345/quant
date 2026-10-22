from datetime import date

import pytest

from quant.data.feed import Bar, InMemoryDataFeed
from quant.instruments.stock import StockConfig


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-network", action="store_true", help="运行需要联网的冒烟测试")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-network"):
        return
    skip = pytest.mark.skip(reason="联网测试默认跳过，用 --run-network 启用")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


def make_bar(open: float, close: float | None = None, adj_close: float | None = None,
             adj_factor: float = 1.0, suspended: bool = False, is_st: bool = False) -> Bar:
    """手工构造一根 bar：未给出的价格按开盘价补齐，后复权价默认等于不复权价 × 复权因子。"""
    close = open if close is None else close
    return Bar(
        open=open,
        high=max(open, close),
        low=min(open, close),
        close=close,
        adj_close=close * adj_factor if adj_close is None else adj_close,
        adj_factor=adj_factor,
        suspended=suspended,
        is_st=is_st,
    )


def stock_costs(**overrides: float) -> StockConfig:
    """佣金、印花税、过户费、滑点全为 0 的股票配置，只打开 overrides 指定的项，便于手算。"""
    zero = dict(commission_rate=0.0, min_commission=0.0,
                stamp_duty_rate=0.0, transfer_fee_rate=0.0, slippage=0.0)
    return StockConfig(**{**zero, **overrides})  # type: ignore[arg-type]


def make_feed(symbol: str, prices: dict[date, tuple[float, float] | Bar]) -> InMemoryDataFeed:
    """prices: 交易日 -> (开盘价, 收盘价) 或现成的 Bar；交易日历即 prices 的键。"""
    bars = {day: p if isinstance(p, Bar) else make_bar(*p)
            for day, p in prices.items()}
    return InMemoryDataFeed(calendar=sorted(prices), bars={symbol: bars})
