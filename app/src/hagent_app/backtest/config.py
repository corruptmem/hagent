"""The ``backtest`` section of ``options.json``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Settings of backtests, trials and invocation records.

    - ``retention_days``: the app keeps invocation records and backtest
      cases for this number of days (design section 10.7). The app never
      uses less than ``max_days``, so that a backtest always finds its
      window.
    - ``default_days``: the window of a backtest when the caller gives none.
    - ``max_days``: the longest window that a caller can ask for.
    - ``max_cases``: the most past invocations that one backtest tests.
    - ``decision_cases``: the default limit for a rule with a decision
      action. Each tested invocation can cost one decision request.
    - ``purge_hours``: the time between two purges of old records.
    """

    retention_days: int = 30
    default_days: int = 7
    max_days: int = 30
    max_cases: int = 1000
    decision_cases: int = 20
    purge_hours: float = 6.0

    @property
    def keep_days(self) -> int:
        """The retention that the app applies."""
        return max(self.retention_days, self.max_days, 1)
