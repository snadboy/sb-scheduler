"""Day-sets — calendar-backed predicates over dates.

A day-set answers questions about DATES, not about now. That distinction is the
whole point of this module: a binary_sensor can only report the present, which
is why scheduler-component ended up with one meaning of "workday" for today and
a different one for every other day.

Eligibility for the whole horizon is precomputed into a set of dates, so both
`is_eligible` and `next_date_on_or_after` are plain lookups. Building that set
costs one `calendar.get_events` call per source calendar per refresh.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BASE_CALENDARS,
    CONF_BASE_DATES,
    CONF_EXCLUDE_CALENDARS,
    CONF_EXCLUDE_DATES,
    CONF_EXCLUDE_MATCH,
    CONF_FORCE_CALENDARS,
    CONF_FORCE_DATES,
    CONF_FORCE_MATCH,
    CONF_ID,
    CONF_INCLUDE_CALENDARS,
    CONF_INCLUDE_DATES,
    CONF_INVERT,
    CONF_NAME,
    CONF_WEEKDAYS,
    HORIZON_DAYS,
    WEEKDAYS,
)

_LOGGER = logging.getLogger(__name__)

RANGE_SEP = ".."


class InvalidDateSpec(ValueError):
    """A hand-entered date or date range could not be parsed."""


def parse_date_spec(raw: str) -> list[tuple[datetime.date, datetime.date]]:
    """Parse "2026-12-25, 2027-06-05..2027-08-17" into inclusive ranges.

    Ranges are what make a summer break one entry instead of sixty, so they are
    first-class rather than an afterthought.
    """
    ranges: list[tuple[datetime.date, datetime.date]] = []
    for token in (t.strip() for t in raw.replace("\n", ",").split(",")):
        if not token:
            continue
        try:
            if RANGE_SEP in token:
                lo_s, hi_s = (p.strip() for p in token.split(RANGE_SEP, 1))
                lo = datetime.date.fromisoformat(lo_s)
                hi = datetime.date.fromisoformat(hi_s)
            else:
                lo = hi = datetime.date.fromisoformat(token)
        except ValueError as err:
            raise InvalidDateSpec(token) from err
        if hi < lo:
            raise InvalidDateSpec(token)
        ranges.append((lo, hi))
    return ranges


def _dates_in_ranges(
    ranges: list[tuple[datetime.date, datetime.date]],
    start: datetime.date,
    end: datetime.date,
) -> set[datetime.date]:
    """Expand inclusive ranges, clipped to [start, end]."""
    out: set[datetime.date] = set()
    for lo, hi in ranges:
        cur = max(lo, start)
        stop = min(hi, end)
        while cur <= stop:
            out.add(cur)
            cur += datetime.timedelta(days=1)
    return out


def _event_matches(event: dict, match: str) -> bool:
    """Case-insensitive substring test against summary AND description.

    Both are needed by real calendars: a days-off entry is distinguished by its
    summary ("Workday"), while Google's holiday feed marks the real ones only
    in the description ("Public holiday" vs "Observance").
    """
    if not match:
        return True
    needle = match.strip().lower()
    haystack = " ".join(
        str(event.get(field) or "") for field in ("summary", "description")
    ).lower()
    return needle in haystack


async def _calendar_dates(
    hass: HomeAssistant,
    entity_id: str,
    start: datetime.date,
    end: datetime.date,
    match: str = "",
) -> set[datetime.date]:
    """Every date covered by any event on a calendar, over [start, end].

    A multi-day all-day event covers [start, end) — that is what lets one
    "Summer break" entry stand in for the whole gap, and it is the detail an
    event-start-only reading would get wrong.
    """
    try:
        response = await hass.services.async_call(
            "calendar",
            "get_events",
            {
                "entity_id": entity_id,
                "start_date_time": dt_util.start_of_local_day(start).isoformat(),
                "end_date_time": dt_util.start_of_local_day(
                    end + datetime.timedelta(days=1)
                ).isoformat(),
            },
            blocking=True,
            return_response=True,
        )
    except Exception:  # noqa: BLE001 - a missing/broken calendar must not kill setup
        _LOGGER.exception("Failed to read events from %s", entity_id)
        return set()

    events = (response or {}).get(entity_id, {}).get("events", [])
    covered: set[datetime.date] = set()
    for event in events:
        if not _event_matches(event, match):
            continue
        lo = _as_date(event.get("start"))
        hi = _as_date(event.get("end"))
        if lo is None:
            continue
        if hi is None:
            hi = lo + datetime.timedelta(days=1)
        # All-day events are half-open: end is the morning after.
        last = hi - datetime.timedelta(days=1) if hi > lo else lo
        cur = max(lo, start)
        while cur <= min(last, end):
            covered.add(cur)
            cur += datetime.timedelta(days=1)
    return covered


def _as_date(value) -> datetime.date | None:
    """Coerce a calendar event boundary to a local date."""
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return dt_util.as_local(value).date()
    if isinstance(value, datetime.date):
        return value
    try:
        parsed = dt_util.parse_datetime(value)
        if parsed is not None:
            return dt_util.as_local(parsed).date()
        return datetime.date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


@dataclass
class DaySet:
    """One named set of dates.

    Precedence is **force > veto > base**. That is not invented: it mirrors the
    workday template helper this replaces, where a days-off entry titled
    "Workday" reinstates a workday, any other days-off entry cancels one, and
    the Workday integration decides the rest.

    Two tiers were not enough. The earlier model called the base tier
    `include`, which outranked `exclude` — so a workday calendar as `include`
    could never be vetoed by days-off, and PTO was silently ignored.
    """

    id: str
    name: str
    weekdays: list[str] = field(default_factory=list)
    base_calendars: list[str] = field(default_factory=list)
    base_dates: str = ""
    force_calendars: list[str] = field(default_factory=list)
    force_dates: str = ""
    force_match: str = ""
    exclude_calendars: list[str] = field(default_factory=list)
    exclude_dates: str = ""
    exclude_match: str = ""
    invert: bool = False

    _eligible: set[datetime.date] = field(default_factory=set, repr=False)
    _window: tuple[datetime.date, datetime.date] | None = field(
        default=None, repr=False
    )

    @classmethod
    def from_config(cls, config: dict) -> DaySet:
        # MIGRATION: `include_*` used to mean the base tier, and every day-set
        # written under the old model meant it that way. Read it as base.
        base_calendars = list(
            config.get(CONF_BASE_CALENDARS)
            or config.get(CONF_INCLUDE_CALENDARS)
            or []
        )
        base_dates = (
            config.get(CONF_BASE_DATES)
            or config.get(CONF_INCLUDE_DATES)
            or ""
        )
        return cls(
            id=config[CONF_ID],
            name=config[CONF_NAME],
            weekdays=list(config.get(CONF_WEEKDAYS) or []),
            base_calendars=base_calendars,
            base_dates=base_dates,
            force_calendars=list(config.get(CONF_FORCE_CALENDARS) or []),
            force_dates=config.get(CONF_FORCE_DATES) or "",
            force_match=config.get(CONF_FORCE_MATCH) or "",
            exclude_calendars=list(config.get(CONF_EXCLUDE_CALENDARS) or []),
            exclude_dates=config.get(CONF_EXCLUDE_DATES) or "",
            exclude_match=config.get(CONF_EXCLUDE_MATCH) or "",
            invert=bool(config.get(CONF_INVERT)),
        )

    @property
    def source_entities(self) -> list[str]:
        """Calendars this day-set reads, so we can re-evaluate when they change."""
        return [*self.base_calendars, *self.force_calendars, *self.exclude_calendars]

    async def async_refresh(
        self, hass: HomeAssistant, start: datetime.date, days: int = HORIZON_DAYS
    ) -> None:
        """Recompute eligibility across the whole window."""
        end = start + datetime.timedelta(days=days)

        async def collect(entities, dates_spec, match=""):
            out: set[datetime.date] = set()
            for entity_id in entities:
                out |= await _calendar_dates(hass, entity_id, start, end, match)
            out |= _dates_in_ranges(parse_date_spec(dates_spec), start, end)
            return out

        base = await collect(self.base_calendars, self.base_dates)
        forced = await collect(
            self.force_calendars, self.force_dates, self.force_match
        )
        vetoed = await collect(
            self.exclude_calendars, self.exclude_dates, self.exclude_match
        )

        mask = {WEEKDAYS.index(d) for d in self.weekdays if d in WEEKDAYS}

        eligible: set[datetime.date] = set()
        cur = start
        while cur <= end:
            if cur in forced:
                hit = True                       # force wins outright
            elif cur in vetoed:
                hit = False                      # veto beats the base tier
            elif cur in base or (mask and cur.weekday() in mask):
                hit = True                       # base: calendars and/or mask
            else:
                hit = False
            if hit != self.invert:
                eligible.add(cur)
            cur += datetime.timedelta(days=1)

        self._eligible = eligible
        self._window = (start, end)

    def is_eligible(self, day: datetime.date) -> bool:
        """Whether `day` is in this set. False outside the computed window."""
        if self._window and not (self._window[0] <= day <= self._window[1]):
            _LOGGER.warning(
                "Day-set '%s' asked about %s, outside its %s..%s window",
                self.id,
                day,
                self._window[0],
                self._window[1],
            )
            return False
        return day in self._eligible

    def next_date_on_or_after(self, day: datetime.date) -> datetime.date | None:
        """The first eligible date at or after `day`.

        Returns None only when the horizon genuinely contains no match, and
        says so loudly — the upstream failure this replaces was a silent None
        that left no timer armed.
        """
        if not self._window:
            return None
        cur = max(day, self._window[0])
        while cur <= self._window[1]:
            if cur in self._eligible:
                return cur
            cur += datetime.timedelta(days=1)
        _LOGGER.error(
            "Day-set '%s' has no eligible date between %s and the %s-day horizon "
            "(%s). Nothing will be scheduled against it.",
            self.id,
            day,
            HORIZON_DAYS,
            self._window[1],
        )
        return None

    def runs(
        self, start: datetime.date, end: datetime.date
    ) -> list[tuple[datetime.date, datetime.date]]:
        """Eligible dates merged into consecutive runs, as inclusive pairs.

        Used by the calendar entity so a Mon-Fri week shows as one block rather
        than five separate all-day events.
        """
        out: list[tuple[datetime.date, datetime.date]] = []
        cur = start
        run_start: datetime.date | None = None
        prev: datetime.date | None = None
        while cur <= end:
            if self.is_eligible(cur):
                if run_start is None:
                    run_start = cur
                prev = cur
            elif run_start is not None:
                out.append((run_start, prev))
                run_start = prev = None
            cur += datetime.timedelta(days=1)
        if run_start is not None and prev is not None:
            out.append((run_start, prev))
        return out
