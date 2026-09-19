"""Next-trigger calculation.

This is the ~250 lines the fork exists to replace. The difference from
scheduler-component is one thing: instead of probing day-by-day with a
hardcoded 16-iteration cap, it asks the day-set for the next eligible date.
A ten-week summer gap therefore costs the same as a one-day gap.
"""

from __future__ import annotations

import datetime
import logging

from .const import (
    CONF_EVERY_MINUTES,
    CONF_OCCURRENCES,
    CONF_PATTERN,
    CONF_START,
    CONF_STOP,
    MAX_INTERVAL_OCCURRENCES,
    PATTERN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# How many eligible DAYS to examine before giving up. Not a look-ahead limit --
# the day-set does the jumping, so this only bounds pathological cases such as
# a schedule with no valid times at all.
MAX_DAYS_EXAMINED = 8


def parse_time(value: str) -> datetime.time | None:
    """Accept "6:30", "06:30" and "06:30:00"."""
    try:
        parts = [int(p) for p in str(value).strip().split(":")]
    except (ValueError, AttributeError):
        return None
    if not 2 <= len(parts) <= 3:
        return None
    try:
        return datetime.time(*parts)
    except ValueError:
        return None


def occurrence_times(pattern: dict) -> list[datetime.time]:
    """The times a schedule fires on any day it runs.

    An interval expands here, at arm-time, rather than in storage -- the stored
    config stays "every 15 minutes from 09:00 to 13:00" instead of seventeen
    timestamps nobody wants to edit.
    """
    if (pattern or {}).get("type") == PATTERN_INTERVAL:
        start = parse_time(pattern.get(CONF_START))
        stop = parse_time(pattern.get(CONF_STOP))
        step = int(pattern.get(CONF_EVERY_MINUTES) or 0)
        if start is None or stop is None or step <= 0:
            _LOGGER.error("Interval pattern is incomplete: %s", pattern)
            return []

        times: list[datetime.time] = []
        cursor = datetime.datetime.combine(datetime.date.min, start)
        end = datetime.datetime.combine(datetime.date.min, stop)
        while cursor <= end and len(times) < MAX_INTERVAL_OCCURRENCES:
            times.append(cursor.time())
            cursor += datetime.timedelta(minutes=step)
        if cursor <= end:
            _LOGGER.warning(
                "Interval pattern capped at %d occurrences (every %d min from "
                "%s to %s) -- later firings that day are dropped",
                MAX_INTERVAL_OCCURRENCES, step, start, stop,
            )
        return times

    times = [parse_time(t) for t in (pattern or {}).get(CONF_OCCURRENCES) or []]
    bad = [t for t, parsed in zip(
        (pattern or {}).get(CONF_OCCURRENCES) or [], times) if parsed is None]
    if bad:
        _LOGGER.error("Ignoring unreadable occurrence time(s): %s", bad)
    return sorted(t for t in times if t is not None)


def next_trigger(
    schedule: dict, day_set, now: datetime.datetime
) -> datetime.datetime | None:
    """The next time this schedule should fire, strictly after `now`.

    `day_set` is anything exposing next_date_on_or_after(date) -> date | None.
    """
    times = occurrence_times(schedule.get(CONF_PATTERN) or {})
    if not times:
        _LOGGER.warning(
            "Schedule '%s' has no usable times; it will never fire",
            schedule.get("name"),
        )
        return None

    day = now.date()
    for _ in range(MAX_DAYS_EXAMINED):
        eligible = day_set.next_date_on_or_after(day)
        if eligible is None:
            # The day-set already logged why. Do not add a second silent None.
            return None
        for moment in times:
            candidate = datetime.datetime.combine(eligible, moment)
            if now.tzinfo is not None:
                candidate = candidate.replace(tzinfo=now.tzinfo)
            if candidate > now:
                return candidate
        # Every time today has passed -- try the next eligible day.
        day = eligible + datetime.timedelta(days=1)

    _LOGGER.error(
        "Schedule '%s' found no trigger within %d eligible days",
        schedule.get("name"),
        MAX_DAYS_EXAMINED,
    )
    return None
