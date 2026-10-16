"""DataFeed：按“日期 × 品种”提供 bar 与交易日历，是回测引擎唯一的数据入口（也是唯一的测试接缝）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True)
class Bar:
    """一个品种一个交易日的行情快照。

    不复权价（open/high/low/close）是交易所当日实际挂牌价，用于撮合与盯市；
    后复权价（adj_close）是把历史除权除息折算进去的连续价格，用于计算信号与
    收益，两者不可混用（见 GLOSSARY「不复权价 / 后复权价」）。

    Attributes:
        open: 开盘价（不复权），撮合价。
        high: 最高价（不复权）。
        low: 最低价（不复权）。
        close: 收盘价（不复权）。
        adj_close: 后复权收盘价，策略信号与指标计算用。
        adj_factor: 后复权因子，除权日跳变、其余交易日沿用原值；是不复权价与
            后复权价之间的换算系数。
        suspended: 当日是否停牌。停牌日仍会返回 Bar（suspended=True），
            「无数据」才表现为 bar() 返回 None。
        is_st: 当日是否为 ST 股。
    """

    open: float
    high: float
    low: float
    close: float
    adj_close: float
    adj_factor: float
    suspended: bool
    is_st: bool


class DataFeed(Protocol):
    """行情数据接口。结构化协议：满足这两个方法签名的类即为 DataFeed，无需显式继承。"""

    def trading_days(self, start: date, end: date) -> list[date]:
        """返回 [start, end] 闭区间内的交易日，升序。

        引擎以该序列驱动逐日回测循环，日历之外的日期不会被访问。

        Args:
            start: 起始日期（含）。
            end: 结束日期（含）。

        Returns:
            区间内全部交易日，按日期升序。
        """
        ...

    def bar(self, symbol: str, day: date) -> Bar | None:
        """返回该品种在该交易日的 bar。

        引擎拿到 None 时跳过该日的撮合与策略调用（如数据缺失、尚未上市）；
        停牌不是 None——停牌日仍有 Bar，由 suspended 字段标识。

        Args:
            symbol: 品种代码。
            day: 交易日。

        Returns:
            当日行情；无数据时为 None。
        """
        ...


class InMemoryDataFeed:
    """内存版 DataFeed：整个区间的日历与 bar 预先加载进内存，查询只走 dict。

    测试用手工构造的 bar 驱动引擎；baostock 等真实数据适配器拉取数据后
    也最终组装成它（见 baostock_feed.load_baostock_feed）。
    """

    def __init__(self, calendar: list[date], bars: dict[str, dict[date, Bar]]):
        """持有日历与全部 bar。

        Args:
            calendar: 全部交易日，无需预先排序（构造时自动排序）。
            bars: 品种 → 交易日 → Bar 的两层嵌套字典。
        """
        self._calendar = sorted(calendar)
        self._bars = bars

    def trading_days(self, start: date, end: date) -> list[date]:
        """见 DataFeed.trading_days。线性扫描已排序日历，筛出闭区间内的日期。"""
        return [d for d in self._calendar if start <= d <= end]

    def bar(self, symbol: str, day: date) -> Bar | None:
        """见 DataFeed.bar。两层 dict.get，品种或日期任一缺失即返回 None。"""
        return self._bars.get(symbol, {}).get(day)
