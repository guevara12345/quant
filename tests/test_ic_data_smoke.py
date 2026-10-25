"""联网冒烟测试：验证中金所 IC 分合约适配器与 AKShare 指数、ETF 适配器。默认跳过，用 pytest --run-network 启用。"""

from datetime import date

import pytest

from quant.data.akshare_feed import load_etf_bars, load_index_bars
from quant.data.cffex_feed import load_cffex_bars


@pytest.mark.network
def test_cffex_ic_bars_by_contract_with_settle(tmp_path):
    bars = load_cffex_bars("IC", date(2024, 1, 1),
                           date(2024, 1, 31), tmp_path)
    # 2024-01 挂牌：当月 IC2401、下月 IC2402、季月 IC2403 与 IC2406
    assert {"IC2401", "IC2402", "IC2403", "IC2406"} <= set(bars)
    bar = bars["IC2403"][date(2024, 1, 2)]
    assert bar.settle is not None and bar.settle > 0
    assert bar.low <= bar.close <= bar.high
    assert date(2024, 1, 6) not in bars["IC2403"]  # 周六


@pytest.mark.network
def test_akshare_csi500_index_bars(tmp_path):
    bars = load_index_bars("sh.000905", date(2024, 1, 1),
                           date(2024, 1, 31), tmp_path)
    assert len(bars) == 22
    assert 4000 < bars[date(2024, 1, 2)].close < 7000


@pytest.mark.network
def test_akshare_500etf_bars_back_adjusted_across_dividend(tmp_path):
    # 2024-05-17 除息（每份 0.087）：不复权价跳空，后复权价连续
    bars = load_etf_bars("sh.510500", date(2024, 5, 1), date(2024, 5, 31), tmp_path)
    before, ex = bars[date(2024, 5, 16)], bars[date(2024, 5, 17)]
    assert 3 < before.close < 10
    assert ex.adj_factor == pytest.approx(before.adj_factor * before.close / (before.close - 0.087))
    assert abs(ex.adj_close / before.adj_close - 1) < 0.03
