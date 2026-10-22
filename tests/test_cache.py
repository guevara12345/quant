"""ParquetCache 按月分段补拉：首次拉取中断后，再次运行从未完成的月份继续。"""

from datetime import date, timedelta

import pandas as pd
import pytest

from quant.data.cache import ParquetCache


class MonthlySource:
    """假数据源：每个自然日一行，记录每次被请求的区间；interrupt_at 指定的区间起点触发中断。"""

    def __init__(self, interrupt_at: date | None = None):
        self.calls: list[tuple[date, date]] = []
        self.interrupt_at = interrupt_at

    def __call__(self, start: date, end: date) -> pd.DataFrame:
        if start == self.interrupt_at:
            raise KeyboardInterrupt
        self.calls.append((start, end))
        days = pd.date_range(start, end).date
        return pd.DataFrame({"date": days, "symbol": "IC", "close": 1.0})


def test_interrupted_first_fetch_resumes_from_unfinished_month(tmp_path):
    start, end = date(2024, 1, 1), date(2024, 4, 30)
    cache = ParquetCache(tmp_path, published_through=date(2024, 12, 31))
    with pytest.raises(KeyboardInterrupt):
        cache.get_by_month("ic", start, end, MonthlySource(interrupt_at=date(2024, 3, 1)),
                           unique_by=("date", "symbol"))

    source = MonthlySource()
    df = ParquetCache(tmp_path, published_through=date(2024, 12, 31)).get_by_month(
        "ic", start, end, source, unique_by=("date", "symbol"))

    assert source.calls == [(date(2024, 3, 1), date(2024, 3, 31)),
                            (date(2024, 4, 1), date(2024, 4, 30))]
    assert list(df["date"]) == [start + timedelta(days=i)
                                for i in range((end - start).days + 1)]
