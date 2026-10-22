from datetime import date

from quant.cli import apply_overrides
from quant.engine import BacktestConfig


def test_set_overrides_nested_stock_config_including_dates():
    config = apply_overrides(
        BacktestConfig(), ["stock.slippage=0", "stock.chinext_reform_date=2021-01-04"])
    assert config.stock.slippage == 0.0
    assert config.stock.chinext_reform_date == date(2021, 1, 4)
