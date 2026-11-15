"""The ``router`` section of ``options.json``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RouterConfig:
    """Retry and wait settings of the router.

    - ``retry_attempts``: the maximum attempts of one chat model request,
      the first included (design section 18).
    - ``retry_base_delay``: the wait before the second attempt, in seconds.
      Each later wait doubles.
    - ``retry_max_delay``: the longest wait between two attempts.
    - ``state_wait_seconds``: after an app start, an invocation waits at
      most this long for the first load of the HA state cache before it
      collects its inputs.
    - ``assist_queue_size``: the queue size of one Assist conversation.
    """

    retry_attempts: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 20.0
    state_wait_seconds: float = 60.0
    assist_queue_size: int = 10
