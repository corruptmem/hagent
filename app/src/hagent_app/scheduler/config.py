"""The ``scheduler`` section of ``options.json``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    """Settings of the scheduler (design section 12).

    - ``missed_run_grace_seconds``: a schedule that was due while the app
      was stopped runs once on start if it is less late than this.
      Otherwise the app skips it and records the skip.
    - ``own_write_hold_seconds``: the longest time that the app holds an
      event while an agent service call waits for its result.
    - ``own_write_ttl_seconds``: how long the app keeps the context ID of a
      finished agent service call.
    - ``timezone``: the IANA time zone of cron schedules. Empty selects the
      local time zone of the app. The Supervisor sets it to the time zone
      of HA.
    - ``job_store``: the file name of the APScheduler job store in the data
      directory.
    - ``retention_days``: the app deletes rows of ``scheduler_runs`` and
      ``scheduler_events`` that are older than this.
    - ``max_changes_per_delivery``: the most events that one subscription
      invocation carries. The invocation also gives the total count.
    """

    missed_run_grace_seconds: float = 600.0
    own_write_hold_seconds: float = 5.0
    own_write_ttl_seconds: float = 60.0
    timezone: str = ""
    job_store: str = "scheduler.sqlite"
    retention_days: int = 30
    max_changes_per_delivery: int = 10
