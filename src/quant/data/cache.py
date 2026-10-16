"""本地 Parquet 缓存：按 key 存一张以 date 列为索引的表，并记录已覆盖的日期区间，只补拉缺失的部分。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger(__name__)

Fetcher = Callable[[date, date], pd.DataFrame]

DATA_PUBLISHED_HOUR = 18  # 北京时间 18 点后认为当日日线已发布


def last_published_day(now: datetime | None = None) -> date:
    """日线数据已经最终发布的最后一天；更晚的日期即使请求了也不记入覆盖区间，以便日后补拉。"""
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    return now.date() if now.hour >= DATA_PUBLISHED_HOUR else now.date() - timedelta(days=1)


class ParquetCache:
    def __init__(self, root: Path, published_through: date | None = None):
        self.root = root
        self.published_through = published_through or last_published_day()
        self.root.mkdir(parents=True, exist_ok=True)
        self._coverage_file = root / "coverage.json"

    def get(self, key: str, start: date, end: date, fetch: Fetcher) -> pd.DataFrame:
        """返回 [start, end] 内的数据；缓存未覆盖的区间调用 fetch 补拉。补拉失败时若已有缓存则降级使用缓存。"""
        cached = self._read(key)
        coverage = self._coverage().get(key)
        missing = [(a, b) for a, b in _missing_ranges(
            start, end, coverage) if a <= self.published_through]
        if missing:
            try:
                fetched = [fetch(a, b) for a, b in missing]
            except Exception as exc:
                if cached is None:
                    raise
                log.warning("补拉 %s %s 失败（%s），使用本地缓存", key, missing, exc)
            else:
                frames = [f for f in [cached, *fetched]
                          if f is not None and not f.empty]
                merged = (pd.concat(frames).drop_duplicates("date", keep="last").sort_values("date")
                          if frames else pd.DataFrame(columns=["date"]))
                self._write(key, merged)
                lo = min(start, date.fromisoformat(
                    coverage[0])) if coverage else start
                hi = min(end, self.published_through)
                hi = max(hi, date.fromisoformat(
                    coverage[1])) if coverage else hi
                self._set_coverage(key, lo, hi)
                cached = merged
        if cached is None:
            raise ValueError(f"{key} 在 {start} 至 {end} 内还没有已发布的数据")
        return cached[(cached["date"] >= start) & (cached["date"] <= end)].reset_index(drop=True)

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.parquet"

    def _read(self, key: str) -> pd.DataFrame | None:
        path = self._path(key)
        if not path.exists():
            return None
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df

    def _write(self, key: str, df: pd.DataFrame) -> None:
        df.to_parquet(self._path(key), index=False)

    def _coverage(self) -> dict[str, list[str]]:
        if not self._coverage_file.exists():
            return {}
        return json.loads(self._coverage_file.read_text(encoding="utf-8"))

    def _set_coverage(self, key: str, start: date, end: date) -> None:
        coverage = self._coverage()
        coverage[key] = [start.isoformat(), end.isoformat()]
        self._coverage_file.write_text(json.dumps(
            coverage, indent=2, sort_keys=True), encoding="utf-8")


def _missing_ranges(start: date, end: date, coverage: list[str] | None) -> list[tuple[date, date]]:
    if coverage is None:
        return [(start, end)]
    lo, hi = date.fromisoformat(coverage[0]), date.fromisoformat(coverage[1])
    missing = []
    if start < lo:
        missing.append((start, lo - timedelta(days=1)))
    if end > hi:
        missing.append((hi + timedelta(days=1), end))
    return missing
