"""Cron expressions for APScheduler (design section 12).

hagent uses standard 5-field cron expressions. APScheduler 3 reads the
day-of-week field differently: ``0`` is Monday, not Sunday, and
``from_crontab`` keeps that. ``cron_trigger`` therefore converts the
day-of-week field to day names first. When both the day of the month and
the day of the week are set, APScheduler fires only when both match.
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo

from apscheduler.triggers.cron import CronTrigger

_DAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


def _expand_days(field: str) -> list[int]:
    """The days of a standard day-of-week field, 0 = Sunday."""
    days: set[int] = set()
    for part in field.split(","):
        base, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if step < 1:
            msg = f"not a valid day-of-week step: {part!r}"
            raise ValueError(msg)
        if base == "*":
            low, high = 0, 6
        elif "-" in base:
            low_text, high_text = base.split("-", 1)
            low, high = int(low_text), int(high_text)
        else:
            low = int(base)
            high = 6 if step_text else low
        if not (0 <= low <= 7 and 0 <= high <= 7 and low <= high):  # noqa: PLR2004
            msg = f"not a valid day-of-week range: {part!r}"
            raise ValueError(msg)
        days.update(day % 7 for day in range(low, high + 1, step))
    return sorted(days)


def cron_trigger(expression: str, timezone: tzinfo) -> CronTrigger:
    """An APScheduler trigger for a standard 5-field cron expression."""
    minute, hour, day, month, weekday = expression.split()
    if weekday != "*":
        weekday = ",".join(_DAY_NAMES[d] for d in _expand_days(weekday))
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=weekday,
        timezone=timezone,
    )


def min_gap_seconds(
    trigger: CronTrigger, now: datetime, *, samples: int = 1500
) -> float | None:
    """The smallest time between two of the next fire times, in seconds.

    The gaps of a cron schedule are not all equal, for example for
    ``0,1 * * * *``. The function checks the fire times of the next two
    days, at most ``samples`` of them. The minute and hour fields repeat
    each day, so two days hold the smallest gap of those fields. Gaps that
    come from the day fields are one day or more. ``None`` means fewer than
    two fire times.
    """
    horizon = now + timedelta(days=2)
    previous: datetime | None = None
    gap: float | None = None
    current = now
    for _ in range(samples):
        fire = trigger.get_next_fire_time(previous, current)
        if fire is None or (fire > horizon and gap is not None):
            break
        if previous is not None:
            seconds = (fire - previous).total_seconds()
            gap = seconds if gap is None else min(gap, seconds)
        previous = fire
        current = fire + timedelta(microseconds=1)
    return gap
