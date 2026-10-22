"""中金所适配器：股指期货按具体合约的日线与结算价，取自中金所官网的月度历史数据包。

AKShare 的 get_cffex_daily 读的是同一个数据包，但它逐日请求、每次都重新下载整月的 zip，
而且写死的浏览器 User-Agent 在部分网络环境下会被安全网关拦截后静默返回空表，所以这里直接按月读取。
对中金所文件格式（列名、编码、文件命名）的依赖集中在本模块。
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

from quant.data.cache import ParquetCache
from quant.data.feed import Bar

MONTHLY_ZIP_URL = "http://www.cffex.com.cn/sj/historysj/{ym}/zip/{ym}.zip"
COLUMNS = {"合约代码": "symbol", "今开盘": "open", "最高价": "high", "最低价": "low",
           "今收盘": "close", "今结算": "settle"}
TIMEOUT = 60


def load_cffex_bars(product: str, start: date, end: date, cache_dir: Path) -> dict[str, dict[date, Bar]]:
    """product（如 "IC"）全部合约在 [start, end] 内的 bar：合约代码 → 交易日 → Bar。"""
    cache = ParquetCache(cache_dir / "cffex")
    df = cache.get_by_month(f"daily_{product}", start, end,
                            lambda a, b: _fetch(product, a, b), unique_by=("date", "symbol"))
    bars: dict[str, dict[date, Bar]] = {}
    for row in df.to_dict("records"):
        close = row["close"]
        # 无成交的合约开高低为空，按收盘价补齐
        o, h, lo = (close if pd.isna(row[k]) else row[k]
                    for k in ("open", "high", "low"))
        bars.setdefault(row["symbol"], {})[row["date"]] = Bar(
            open=o, high=h, low=lo, close=close, adj_close=close, adj_factor=1.0,
            # 结算价缺失时记为 None：当日不盯市，盈亏顺延到下一个有结算价的交易日
            suspended=False, is_st=False, settle=None if pd.isna(row["settle"]) else row["settle"])
    return bars


def _fetch(product: str, start: date, end: date) -> pd.DataFrame:
    frames = []
    for ym in sorted({d.strftime("%Y%m") for d in pd.date_range(start, end).date}):
        resp = requests.get(MONTHLY_ZIP_URL.format(ym=ym), timeout=TIMEOUT)
        resp.raise_for_status()
        if not resp.content.startswith(b"PK"):
            raise RuntimeError(f"中金所 {ym} 数据包不是 zip（可能被网关拦截或尚未发布）")
        with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
            for name in archive.namelist():
                # 文件名形如 20240102_1.csv
                day = datetime.strptime(name[:8], "%Y%m%d").date()
                if start <= day <= end:
                    frames.append(_parse(archive.read(name), day, product))
    columns = ["date", *COLUMNS.values()]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)


def _parse(content: bytes, day: date, product: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(content.decode("gbk")))
    df.columns = [c.strip() for c in df.columns]
    df = df[list(COLUMNS)].rename(columns=COLUMNS)
    df["symbol"] = df["symbol"].astype(str).str.strip()
    # 只保留期货合约（如 IC2401），排除同品种前缀的期权与小计行
    df = df[df["symbol"].str.fullmatch(rf"{product}\d{{4}}")].copy()
    for col in ["open", "high", "low", "close", "settle"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.insert(0, "date", day)
    return df
