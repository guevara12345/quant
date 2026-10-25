"""期权定价与合约规则的纯函数（ticket 05）：行权价网格、BS 模型价与 delta。

这些是策略（06）直接调用的公开函数，单独测；期权的成交、估值、到期结算经引擎测（test_option_rules.py）。
"""

import math

import pytest

from quant.calendars import strike_at_or_below
from quant.pricing import implied_vol, put_delta, put_price


@pytest.mark.parametrize("price, strike", [
    (2.87, 2.85),   # 3 元以下间距 0.05
    (3.00, 3.00),
    (3.07, 3.00),   # 3–5 元间距 0.1
    (4.99, 4.90),
    (5.00, 5.00),
    (5.24, 5.00),   # 5–10 元间距 0.25
    (6.40, 6.25),
    (10.40, 10.00),  # 10–20 元间距 0.5
    (23.70, 23.00),  # 20–50 元间距 1
    (57.00, 55.00),  # 50–100 元间距 2.5
    (103.00, 100.00),  # 100 元以上间距 5
])
def test_strike_is_nearest_grid_strike_at_or_below_etf_price(price, strike):
    assert strike_at_or_below(price) == pytest.approx(strike)


R, Q, VOL = 0.015, 0.015, 0.25


@pytest.mark.parametrize("spot", [3.0, 4.5, 5.0, 5.5, 8.0])
@pytest.mark.parametrize("years", [1 / 365, 30 / 365, 1.0])
def test_put_model_price_lies_within_no_arbitrage_bounds(spot, years):
    # 欧式 put：K·e^{-rT} − S·e^{-qT} ≤ 价格 ≤ K·e^{-rT}（行权价 5）
    strike = 5.0
    price = put_price(spot, strike, years, R, Q, VOL)
    lower = max(strike * math.exp(-R * years) - spot * math.exp(-Q * years), 0.0)
    assert lower - 1e-12 <= price <= strike * math.exp(-R * years)


def test_put_model_price_matches_hand_computed_black_scholes_value():
    # S = K = 5、T = 1、r = q = 1.5%、σ = 25%：d1 = 0.125、d2 = −0.125
    # put = 5·e^{−0.015}·(N(0.125) − N(−0.125)) = 4.92556 × 0.099476 = 0.48998
    assert put_price(5.0, 5.0, 1.0, R, Q, VOL) == pytest.approx(0.48998, abs=1e-5)


def test_put_at_expiry_is_worth_its_intrinsic_value():
    assert put_price(4.6, 5.0, 0.0, R, Q, VOL) == pytest.approx(0.4)
    assert put_price(5.4, 5.0, 0.0, R, Q, VOL) == 0.0


def test_put_with_zero_volatility_is_worth_its_discounted_forward_intrinsic_value():
    # σ = 0：到期时标的确定为 S·e^{(r−q)T}，put 价值 = max(K·e^{−rT} − S·e^{−qT}, 0)
    assert put_price(4.6, 5.0, 1.0, R, Q, 0.0) == pytest.approx(0.4 * math.exp(-0.015))
    assert put_price(5.4, 5.0, 1.0, R, Q, 0.0) == 0.0
    assert put_delta(4.6, 5.0, 1.0, R, Q, 0.0) == pytest.approx(-math.exp(-0.015))
    assert put_delta(5.4, 5.0, 1.0, R, Q, 0.0) == 0.0


@pytest.mark.parametrize("spot", [2.0, 4.5, 5.0, 5.5, 9.0])
def test_put_delta_is_between_minus_one_and_zero(spot):
    assert -1.0 <= put_delta(spot, 5.0, 30 / 365, R, Q, VOL) <= 0.0


def test_at_the_money_put_delta_is_close_to_minus_one_half():
    assert put_delta(5.0, 5.0, 30 / 365, R, Q, VOL) == pytest.approx(-0.5, abs=0.02)


def test_implied_vol_recovers_the_volatility_behind_a_model_price():
    # 已知 S = K = 5、T = 1、σ = 25% 时模型价 0.48998（见上），反推回 25%
    assert implied_vol(0.48998, 5.0, 5.0, 1.0, R, Q) == pytest.approx(0.25, abs=1e-4)
    # 虚值、短期限的报价同样能反推
    price = put_price(5.6, 5.0, 20 / 365, R, Q, 0.4)
    assert implied_vol(price, 5.6, 5.0, 20 / 365, R, Q) == pytest.approx(0.4, abs=1e-6)
