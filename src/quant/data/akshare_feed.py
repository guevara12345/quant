"""AKShare 适配器：指数日线（如中证 500），经 Parquet 缓存后转成 Bar。对 AKShare 接口名与字段名的依赖集中在本模块。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from quant.data.cache import ParquetCache
from quant.data.feed import Bar


def load_index_bars(symbol: str, start: date, end: date, cache_dir: Path) -> dict[date, Bar]:
    """symbol 用 baostock 风格的代码，如 "sh.000905"。指数没有复权，后复权价即收盘价。"""
    cache = ParquetCache(cache_dir / "akshare")
    df = cache.get(f"index_{symbol}", start, end,
                   lambda a, b: _fetch_index(symbol, a, b))
    return {
        row["date"]: Bar(open=row["open"], high=row["high"], low=row["low"], close=row["close"],
                         adj_close=row["close"], adj_factor=1.0, suspended=False, is_st=False)
        for row in df.to_dict("records")
    }


def _fetch_index(symbol: str, start: date, end: date) -> pd.DataFrame:
    import akshare as ak

    # 新浪接口一次返回全部历史，按区间截取
    df = ak.stock_zh_index_daily(symbol=symbol.replace(".", ""))
    out = pd.DataFrame({"date": pd.to_datetime(df["date"]).dt.date})
    for col in ["open", "high", "low", "close"]:
        out[col] = pd.to_numeric(df[col])
    return out[(out["date"] >= start) & (out["date"] <= end)].reset_index(drop=True)
