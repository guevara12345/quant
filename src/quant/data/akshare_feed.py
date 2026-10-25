"""AKShare 适配器：指数日线（如中证 500）、ETF 日线（如 510500），经 Parquet 缓存后转成 Bar。对 AKShare 接口名与字段名的依赖集中在本模块。"""

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


def load_etf_bars(symbol: str, start: date, end: date, cache_dir: Path) -> dict[date, Bar]:
    """symbol 用 baostock 风格的代码，如 "sh.510500"。

    价格为不复权价；后复权因子由新浪的累计分红推算（baostock 给这只 ETF 的复权因子有误，不用）：
    每个除息日因子 × 前收 / (前收 − 每份分红)。份额折算（如 510500 在 2015-04 的折算）不在分红表中，未处理。
    """
    cache = ParquetCache(cache_dir / "akshare")
    df = cache.get(f"etf_{symbol}", start, end,
                   lambda a, b: _fetch_etf(symbol, a, b))
    return {
        row["date"]: Bar(open=row["open"], high=row["high"], low=row["low"], close=row["close"],
                         adj_close=row["close"] * row["adj_factor"], adj_factor=row["adj_factor"],
                         suspended=False, is_st=False)
        for row in df.to_dict("records")
    }


def _fetch_etf(symbol: str, start: date, end: date) -> pd.DataFrame:
    import akshare as ak

    # 新浪接口一次返回全部历史；因子从上市起累乘，按区间截取后与已缓存的部分一致
    code = symbol.replace(".", "")
    df = ak.fund_etf_hist_sina(symbol=code)
    out = pd.DataFrame({"date": pd.to_datetime(df["date"]).dt.date})
    for col in ["open", "high", "low", "close"]:
        out[col] = pd.to_numeric(df[col])
    dividends = ak.fund_etf_dividend_sina(symbol=code)
    cumulative = pd.to_numeric(dividends["累计分红"])
    per_share = dict(zip(pd.to_datetime(dividends["日期"]).dt.date,
                         cumulative.diff().fillna(cumulative)))
    factor, factors = 1.0, []
    prev_close: float | None = None
    for day, close in zip(out["date"], out["close"]):
        div = per_share.get(day, 0.0)
        if div > 0 and prev_close is not None:
            factor *= prev_close / (prev_close - div)
        factors.append(factor)
        prev_close = close
    out["adj_factor"] = factors
    return out[(out["date"] >= start) & (out["date"] <= end)].reset_index(drop=True)
