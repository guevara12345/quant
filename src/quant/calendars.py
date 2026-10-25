"""合约日历与合约规则：股指期货到期日、季月合约选取，ETF 期权到期日与行权价网格。交易日历由调用方以函数形式传入（通常是 DataFeed.trading_days）。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

TradingDays = Callable[[date, date], list[date]]

QUARTER_MONTHS = (3, 6, 9, 12)


def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    # weekday()：周一为 0，周五为 4
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def future_expiry(year: int, month: int, trading_days: TradingDays) -> date:
    """股指期货到期日：到期月份第三个周五，遇节假日顺延到下一个交易日。

    第三个周五之后不在已知交易日历内时（如日历只到今天），按第三个周五计。
    """
    friday = third_friday(year, month)
    following = trading_days(friday, friday + timedelta(days=30))
    return following[0] if following else friday


def contract_symbol(product: str, year: int, month: int) -> str:
    """如 ("IC", 2024, 12) → "IC2412"。"""
    return f"{product}{year % 100:02d}{month:02d}"


def quarterly_contracts(product: str, day: date, count: int) -> list[tuple[str, int, int]]:
    """从 day 所在月份起（含当月）的 count 个季月合约：(代码, 年, 月)，按到期先后排列。"""
    out: list[tuple[str, int, int]] = []
    year, month = day.year, day.month
    while len(out) < count:
        if month in QUARTER_MONTHS:
            out.append((contract_symbol(product, year, month), year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def weekdays_after(day: date, until: date) -> list[date]:
    """(day, until] 内的周一至周五，用来把交易日历延伸到尚未公布的未来（忽略节假日）。"""
    out: list[date] = []
    d = day + timedelta(days=1)
    while d <= until:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# 上交所 ETF 期权行权价间距，按行权价所在区间分档：(区间上限（含）, 间距)，单位厘（0.001 元），避免浮点误差
STRIKE_STEPS_MILLI = ((3_000, 50), (5_000, 100), (10_000, 250),
                      (20_000, 500), (50_000, 1_000), (100_000, 2_500))
STRIKE_STEP_ABOVE_MILLI = 5_000  # 100 元以上


def strike_at_or_below(price: float) -> float:
    """行权价网格上不高于 price 的最近一档（平值 put 的行权价）。

    间距取 price 所在档；各档边界（3、5、10、20、50、100 元）都是高一档间距的整数倍，
    所以向下取整不会落到网格之外。
    """
    milli = round(price * 1000)
    step = next((s for upper, s in STRIKE_STEPS_MILLI if milli <= upper), STRIKE_STEP_ABOVE_MILLI)
    return milli // step * step / 1000


def fourth_wednesday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(2 - first.weekday()) % 7 + 21)


def option_expiry(year: int, month: int, trading_days: TradingDays) -> date:
    """上交所 ETF 期权到期日：到期月份第四个星期三，遇节假日顺延到下一个交易日。

    与 future_expiry 一样，第四个星期三之后不在已知交易日历内时按第四个星期三计。
    """
    wednesday = fourth_wednesday(year, month)
    following = trading_days(wednesday, wednesday + timedelta(days=30))
    return following[0] if following else wednesday
