"""baostock 适配器：A 股日线（不复权、后复权）、复权因子、停牌与 ST 状态、交易日历，经 Parquet 缓存后组装成 DataFeed。

对 baostock 字段名和取值约定的依赖集中在本模块。
"""

from __future__ import annotations

import contextlib
import io
from collections.abc import Iterator
from datetime import date
from functools import partial
from pathlib import Path

import pandas as pd

from quant.data.cache import ParquetCache
from quant.data.feed import Bar, InMemoryDataFeed

K_FIELDS = "date,open,high,low,close,tradestatus,isST"
ADJUST_NONE = "3"  # baostock adjustflag：1 后复权，2 前复权，3 不复权
ADJUST_BACKWARD = "1"


def load_baostock_feed(symbols: list[str], start: date, end: date, cache_dir: Path) -> InMemoryDataFeed:
    cache = ParquetCache(cache_dir / "baostock")
    with _session() as source:
        calendar = cache.get("trade_dates", start, end, source.trade_dates)
        bars = {}
        for symbol in symbols:
            unadjusted = cache.get(f"k_{symbol}_unadjusted", start, end, partial(
                source.k_data, symbol, adjustflag=ADJUST_NONE))
            backward_adjusted = cache.get(f"k_{symbol}_backward_adjusted", start, end, partial(
                source.k_data, symbol, adjustflag=ADJUST_BACKWARD))
            # 复权因子只在除权日有记录，需要从最早的记录开始取，才能前向填充到区间起点
            factors = cache.get(f"adjust_factor_{symbol}", date(
                1990, 1, 1), end, partial(source.adjust_factor, symbol))
            bars[symbol] = _to_bars(unadjusted, backward_adjusted, factors)
    trading_days = calendar.loc[calendar["is_trading_day"], "date"].tolist()
    return InMemoryDataFeed(calendar=trading_days, bars=bars)


def _to_bars(unadjusted: pd.DataFrame, backward_adjusted: pd.DataFrame, factors: pd.DataFrame) -> dict[date, Bar]:
    df = unadjusted.merge(backward_adjusted[["date", "close"]].rename(
        columns={"close": "adj_close"}), on="date", how="left")
    df = df.sort_values("date")
    factor = pd.Series(factors["back_adjust_factor"].to_numpy(
    ), index=factors["date"]).sort_index()
    df["adj_factor"] = [_factor_on(factor, d) for d in df["date"]]
    return {
        row["date"]: Bar(
            open=row["open"], high=row["high"], low=row["low"], close=row["close"],
            adj_close=row["adj_close"], adj_factor=row["adj_factor"],
            suspended=bool(row["suspended"]), is_st=bool(row["is_st"]),
        )
        for row in df.to_dict("records")
    }


def _factor_on(factor: pd.Series, day: date) -> float:
    earlier = factor[factor.index <= day]
    return float(earlier.iloc[-1]) if len(earlier) else 1.0


class _BaostockSource:
    """懒登录：只有缓存缺数据、真正发起查询时才登录 baostock，断网时完全命中缓存的回测不受影响。"""

    def __init__(self) -> None:
        self._bs = None

    def trade_dates(self, start: date, end: date) -> pd.DataFrame:
        df = self._query("query_trade_dates",
                         start_date=start.isoformat(), end_date=end.isoformat())
        return pd.DataFrame({
            "date": pd.to_datetime(df["calendar_date"]).dt.date,
            "is_trading_day": df["is_trading_day"] == "1",
        })

    def k_data(self, symbol: str, start: date, end: date, adjustflag: str) -> pd.DataFrame:
        df = self._query("query_history_k_data_plus", code=symbol, fields=K_FIELDS,
                         start_date=start.isoformat(), end_date=end.isoformat(),
                         frequency="d", adjustflag=adjustflag)
        out = pd.DataFrame({"date": pd.to_datetime(df["date"]).dt.date})
        for col in ["open", "high", "low", "close"]:
            out[col] = pd.to_numeric(df[col])
        out["suspended"] = df["tradestatus"] == "0"
        out["is_st"] = df["isST"] == "1"
        return out

    def adjust_factor(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        df = self._query("query_adjust_factor", code=symbol,
                         start_date=start.isoformat(), end_date=end.isoformat())
        return pd.DataFrame({
            "date": pd.to_datetime(df["dividOperateDate"]).dt.date,
            "back_adjust_factor": pd.to_numeric(df["backAdjustFactor"]),
        })

    def _query(self, name: str, **kwargs: str) -> pd.DataFrame:
        bs = self._login()
        rs = getattr(bs, name)(**kwargs)
        if rs.error_code != "0":
            raise RuntimeError(
                f"baostock {name} 失败：{rs.error_code} {rs.error_msg}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        return pd.DataFrame(rows, columns=rs.fields)

    def _login(self):  # type: ignore[no-untyped-def]
        if self._bs is None:
            import baostock as bs

            with contextlib.redirect_stdout(io.StringIO()):
                lg = bs.login()
            if lg.error_code != "0":
                raise RuntimeError(
                    f"baostock 登录失败：{lg.error_code} {lg.error_msg}")
            self._bs = bs
        return self._bs

    def close(self) -> None:
        if self._bs is not None:
            with contextlib.redirect_stdout(io.StringIO()):
                self._bs.logout()


@contextlib.contextmanager
def _session() -> Iterator[_BaostockSource]:
    source = _BaostockSource()
    try:
        yield source
    finally:
        source.close()
