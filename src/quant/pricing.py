"""期权定价：欧式 Black-Scholes 模型价与 delta（带连续股息率），以及标的 ETF 的已实现波动率。

回测中的期权价格一律用模型价（ADR-0002）。时间以年计，调用方按自然日 / 365 折算。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist, stdev

_N = NormalDist().cdf
TRADING_DAYS_PER_YEAR = 252


def _d1_d2(spot: float, strike: float, years: float, rate: float, dividend: float,
           vol: float) -> tuple[float, float]:
    d1 = (math.log(spot / strike) + (rate - dividend + vol * vol / 2) * years) / (vol * math.sqrt(years))
    return d1, d1 - vol * math.sqrt(years)


def put_price(spot: float, strike: float, years: float, rate: float, dividend: float,
              vol: float) -> float:
    """每份标的的 put 模型价；到期时（years ≤ 0）等于内在价值 max(K − S, 0)，
    波动率为 0 时等于贴现后的远期内在价值 max(K·e^{−rT} − S·e^{−qT}, 0)。"""
    if years <= 0:
        return max(strike - spot, 0.0)
    if vol <= 0:
        return max(strike * math.exp(-rate * years) - spot * math.exp(-dividend * years), 0.0)
    d1, d2 = _d1_d2(spot, strike, years, rate, dividend, vol)
    return strike * math.exp(-rate * years) * _N(-d2) - spot * math.exp(-dividend * years) * _N(-d1)


def put_delta(spot: float, strike: float, years: float, rate: float, dividend: float,
              vol: float) -> float:
    """put 对标的价格的 delta，在 −1 与 0 之间；到期时实值为 −1、否则为 0，波动率为 0 时按远期是否实值同理。"""
    if years <= 0:
        return -1.0 if spot < strike else 0.0
    if vol <= 0:
        in_the_money = strike * math.exp(-rate * years) > spot * math.exp(-dividend * years)
        return -math.exp(-dividend * years) if in_the_money else 0.0
    d1, _ = _d1_d2(spot, strike, years, rate, dividend, vol)
    return -math.exp(-dividend * years) * _N(-d1)


def implied_vol(price: float, spot: float, strike: float, years: float, rate: float, dividend: float,
                low: float = 1e-4, high: float = 5.0) -> float:
    """由 put 价格反推波动率（二分法，put 价格随波动率单调递增）。

    价格低于 low 对应的价格时返回 low，高于 high 对应的价格时返回 high。
    """
    for _ in range(100):
        mid = (low + high) / 2
        if put_price(spot, strike, years, rate, dividend, mid) < price:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def realized_vol(closes: Sequence[float]) -> float:
    """按收盘价序列的对数收益率计算年化已实现波动率（样本标准差 × √252）。"""
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    return stdev(returns) * math.sqrt(TRADING_DAYS_PER_YEAR)
