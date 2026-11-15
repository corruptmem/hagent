"""Backtests, trials and invocation records (design sections 10.1, 10.7).

See ``app/README.md`` (section "Backtests and trials").
"""

from hagent_app.backtest.component import BacktestComponent
from hagent_app.backtest.config import BacktestConfig
from hagent_app.backtest.runner import Backtester, BacktestError, BacktestOptions

__all__ = [
    "BacktestComponent",
    "BacktestConfig",
    "BacktestError",
    "BacktestOptions",
    "Backtester",
]
