"""联网冒烟测试：验证 baostock 适配器能拉到数据并组装成 DataFeed。默认跳过，用 pytest --run-network 启用。"""

from datetime import date

import pytest

from quant.data.baostock_feed import load_baostock_feed


@pytest.mark.network
def test_baostock_feed_loads_moutai(tmp_path):
    feed = load_baostock_feed(["sh.600519"], date(2024, 6, 1), date(2024, 6, 30), tmp_path)
    days = feed.trading_days(date(2024, 6, 1), date(2024, 6, 30))
    assert date(2024, 6, 10) not in days  # 端午节
    assert len(days) == 19
    bar = feed.bar("sh.600519", days[0])
    assert bar is not None and bar.close > 0
    assert bar.adj_close > bar.close  # 后复权价高于不复权价
    assert bar.adj_factor > 1
    assert not bar.suspended and not bar.is_st
