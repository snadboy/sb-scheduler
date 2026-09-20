"""Next-trigger calculation.

This is the core the fork exists to replace. Two differences from
scheduler-component:

1. Instead of probing day-by-day under a hardcoded 16-iteration cap, it asks
   the day-set for the next eligible date, so a ten-week gap costs the same as
   a one-day gap.
2. Sun times are resolved **per date**, not read from `sun.next_rising`.
   Upstream's source only knows the next sunrise, which is the same
   "now-only source" flaw as the workday sensor.

No Home Assistant imports: sun resolution arrives as a callback, so the whole
module is testable offline.
"""

from __future__ import annotations

import datetime
import logging
import re

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

SUNRISE = "sunrise"
SUNSET = "sunset"

# "sunset", "sunset+00:15", "sunrise-01:30:00"
SUN_PATTERN = re.compile(
    r"^(sunrise|sunset)(?:\s*([+-])\s*(\d{1,2}:\d{2}(?::\d{2})?))?$", re.IGNORECASE
)


class Occurrence:
    """One time-of-day a schedule fires, fixed or relative to the sun."""

    def __init__(self, raw: str, fixed=None, event=None, offset=None):
        self.raw = raw
        self.fixed = fixed          # datetime.time, or None for a sun occurrence
        self.event = event          # "sunrise" / "sunset"
        self.offset = offset or datetime.timedelta()

    @property
    def is_sun(self) -> bool:
        return self.event is not None

    def resolve(self, day: datetime.date, sun_resolver=None) -> datetime.datetime | None:
        """The datetime this occurrence lands on for `day`."""
        if not self.is_sun:
            return datetime.datetime.combine(day, self.fixed)
        if sun_resolver is None:
            _LOGGER.error(
                "Occurrence '%s' needs sun times but no resolver was supplied", self.raw
            )
            return None
        base = sun_resolver(self.event, day)
        if base is None:
            # Polar day/night, or the sun component is unavailable.
            _LOGGER.warning("No %s on %s; skipping occurrence '%s'", self.event, day, self.raw)
            return None
        # Astral is precise to the microsecond; second-level precision is
        # meaningless for a schedule and makes the display noisy.
        return (base + self.offset).replace(second=0, microsecond=0)

    def __repr__(self):
        return f"<Occurrence {self.raw}>"


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


def parse_occurrence(value: str) -> Occurrence | None:
    """A fixed time, or sunrise/sunset with an optional offset."""
    raw = str(value).strip()
    match = SUN_PATTERN.match(raw)
    if match:
        event = match.group(1).lower()
        offset = datetime.timedelta()
        if match.group(3):
            parsed = parse_time(match.group(3))
            if parsed is None:
                return None
            offset = datetime.timedelta(
                hours=parsed.hour, minutes=parsed.minute, seconds=parsed.second
            )
            if match.group(2) == "-":
                offset = -offset
        return Occurrence(raw, event=event, offset=offset)

    fixed = parse_time(raw)
    return Occurrence(raw, fixed=fixed) if fixed is not None else None


def occurrences(pattern: dict) -> list[Occurrence]:
    """The occurrences a schedule fires on any day it runs.

    An interval expands here, at arm-time, rather than in storage -- the stored
    config stays "every 15 minutes from 09:00 to 13:00" instead of seventeen
    timestamps nobody wants to edit. Intervals are fixed-time only.
    """
    pattern = pattern or {}
    if pattern.get("type") == PATTERN_INTERVAL:
        start = parse_time(pattern.get(CONF_START))
        stop = parse_time(pattern.get(CONF_STOP))
        step = int(pattern.get(CONF_EVERY_MINUTES) or 0)
        if start is None or stop is None or step <= 0:
            _LOGGER.error("Interval pattern is incomplete: %s", pattern)
            return []

        out: list[Occurrence] = []
        cursor = datetime.datetime.combine(datetime.date.min, start)
        end = datetime.datetime.combine(datetime.date.min, stop)
        while cursor <= end and len(out) < MAX_INTERVAL_OCCURRENCES:
            out.append(Occurrence(cursor.time().isoformat(), fixed=cursor.time()))
            cursor += datetime.timedelta(minutes=step)
        if cursor <= end:
            _LOGGER.warning(
                "Interval pattern capped at %d occurrences (every %d min from "
                "%s to %s) -- later firings that day are dropped",
                MAX_INTERVAL_OCCURRENCES, step, start, stop,
            )
        return out

    out = []
    for raw in pattern.get(CONF_OCCURRENCES) or []:
        parsed = parse_occurrence(raw)
        if parsed is None:
            _LOGGER.error("Ignoring unreadable occurrence time: %r", raw)
        else:
            out.append(parsed)
    return out


def _resolved(
    pattern: dict, day: datetime.date, sun_resolver=None, tzinfo=None
) -> list[tuple[datetime.datetime, Occurrence]]:
    """Each occurrence paired with the moment it lands on, sorted by moment.

    Fixed times resolve NAIVE while a sun resolver backed by Home Assistant
    returns AWARE datetimes, and sorting a mixed list raises TypeError. A
    schedule mixing "00:00" with "sunset+00:15" hits this; one with only a sun
    time does not, because a one-element sort never compares. So normalise
    before sorting rather than relying on callers to pass a tz.

    Pairing matters: the list is sorted by resolved moment, so an occurrence's
    position here does NOT match its position in the stored pattern. Anything
    wanting to describe a time must carry the occurrence with it.
    """
    pairs = []
    for occurrence in occurrences(pattern):
        moment = occurrence.resolve(day, sun_resolver)
        if moment is None:
            continue
        if tzinfo is not None and moment.tzinfo is None:
            moment = moment.replace(tzinfo=tzinfo)
        pairs.append((moment, occurrence))

    aware = [m.tzinfo for m, _ in pairs if m.tzinfo is not None]
    if aware and len(aware) != len(pairs):
        pairs = [
            (m if m.tzinfo else m.replace(tzinfo=aware[0]), o) for m, o in pairs
        ]
    return sorted(pairs, key=lambda pair: pair[0])


def times_on(
    pattern: dict, day: datetime.date, sun_resolver=None, tzinfo=None
) -> list[datetime.datetime]:
    """Every firing time on a given day, sorted.

    Sun occurrences have to be resolved per date -- sunset moves, so the order
    of occurrences is not fixed across the year either.
    """
    return [moment for moment, _ in _resolved(pattern, day, sun_resolver, tzinfo)]


def describe_times(
    pattern: dict, day: datetime.date, sun_resolver=None, tzinfo=None
) -> list[dict]:
    """Firing times plus WHERE each came from, for display.

    A resolved "06:50" tells you nothing about whether it tracks sunrise. The
    card needs both, and cannot zip `times` against the stored occurrences
    because resolution reorders them.
    """
    out = []
    for moment, occurrence in _resolved(pattern, day, sun_resolver, tzinfo):
        out.append({
            "time": moment.strftime("%H:%M"),
            "event": occurrence.event,
            "offset_minutes": int(occurrence.offset.total_seconds() // 60),
        })
    return out


def next_trigger(
    pattern: dict, day_set, now: datetime.datetime, sun_resolver=None, label: str = ""
) -> datetime.datetime | None:
    """The next time this PATTERN should fire, strictly after `now`.

    Takes a pattern rather than a whole schedule, because a schedule now has
    one pattern per step and the caller picks the earliest across them.

    `day_set` is anything exposing next_date_on_or_after(date) -> date | None.
    `sun_resolver(event, date) -> datetime | None` supplies sunrise/sunset.
    """
    pattern = pattern or {}
    if not occurrences(pattern):
        _LOGGER.warning("'%s' has no usable times; it will never fire", label)
        return None

    day = now.date()
    for _ in range(MAX_DAYS_EXAMINED):
        eligible = day_set.next_date_on_or_after(day)
        if eligible is None:
            # The day-set already logged why. Do not add a second silent None.
            return None
        for candidate in times_on(pattern, eligible, sun_resolver, now.tzinfo):
            if candidate > now:
                return candidate
        # Every time today has passed -- try the next eligible day.
        day = eligible + datetime.timedelta(days=1)

    _LOGGER.error(
        "'%s' found no trigger within %d eligible days", label, MAX_DAYS_EXAMINED
    )
    return None
