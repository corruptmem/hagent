"""The ``rules`` section of ``options.json``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RulesConfig:
    """Settings of the rule engine.

    - ``decision_retry_attempts``: the maximum attempts of one decision
      request, the first included (design section 18).
    - ``decision_retry_base_delay``: the wait before the second attempt, in
      seconds. Each later wait doubles, up to ``decision_retry_max_delay``.
    - ``render_seconds``: the time limit of one template render.
    - ``cache_entries``: the maximum entries of the rule result cache and of
      the context cache.
    """

    decision_retry_attempts: int = 3
    decision_retry_base_delay: float = 1.0
    decision_retry_max_delay: float = 20.0
    render_seconds: float = 1.0
    cache_entries: int = 1000
