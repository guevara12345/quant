from datetime import date

import pytest

from quant.data.feed import Bar, InMemoryDataFeed


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-network", action="store_true", help="运行需要联网的冒烟测试")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-network"):
        return
    skip = pytest.mark.skip(reason="联网测试默认跳过，用 --run-network 启用")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


def make_bar(open: float, close: float | None = None, adj_close: float | None = None) -> Bar:
    """手工构造一根 bar：未给出的价格按开盘价补齐，后复权价默认等于不复权价。"""
    close = open if close is None else close
    return Bar(
        open=open,
        high=max(open, close),
        low=min(open, close),
        close=close,
        adj_close=close if adj_close is None else adj_close,
        adj_factor=1.0,
        suspended=False,
        is_st=False,
    )


def make_feed(symbol: str, prices: dict[date, tuple[float, float]]) -> InMemoryDataFeed:
    """prices: 交易日 -> (开盘价, 收盘价)；交易日历即 prices 的键。"""
    bars = {day: make_bar(o, c) for day, (o, c) in prices.items()}
    return InMemoryDataFeed(calendar=sorted(prices), bars={symbol: bars})
