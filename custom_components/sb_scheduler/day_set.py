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
    CONF_BASE_DAY_SET,
    CONF_EXCLUDE_CALENDARS,
    CONF_EXCLUDE_DATES,
    CONF_EXCLUDE_MATCH,
    CONF_EXPOSE_CALENDAR,
    CONF_FORCE_CALENDARS,
    CONF_FORCE_DATES,
    CONF_FORCE_MATCH,
    CONF_ID,
    CONF_INCLUDE_CALENDARS,
    CONF_INCLUDE_DATES,
    CONF_INVERT,
    CONF_MONTHS,
    CONF_NAME,
    CONF_OFFSET_DAYS,
    CONF_PICK,
    CONF_PICK_ANCHOR,
    CONF_PICK_EVERY,
    CONF_PICK_NTH,
    CONF_WEEKDAYS,
    HORIZON_DAYS,
    PICK_EVERY,
    PICK_NONE,
    PICK_NTH_LAST,
    PICK_NTH_OF_MONTH,
    WEEKDAYS,
)

_LOGGER = logging.getLogger(__name__)

RANGE_SEP = ".."


def order_by_dependency(configs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Sort day-set configs so every base_day_set is evaluated before its user.

    Returns (ordered, unresolved). Anything in `unresolved` names a base that
    does not exist or sits on a cycle; the caller evaluates it with an empty
    base and says so, rather than hanging or silently producing nothing.
    """
    by_id = {c[CONF_ID]: c for c in configs}
    ordered: list[dict] = []
    done: set[str] = set()
    pending = list(configs)
    while pending:
        progressed = False
        for cfg in list(pending):
            base = cfg.get(CONF_BASE_DAY_SET) or ""
            if not base or base in done:
                ordered.append(cfg)
                done.add(cfg[CONF_ID])
                pending.remove(cfg)
                progressed = True
            elif base not in by_id:
                # Missing base: nothing to wait for. Evaluate it anyway.
                return ordered + [cfg] + [p for p in pending if p is not cfg], [cfg]
        if not progressed:
            # Every remaining config waits on another remaining one: a cycle.
            return ordered + pending, list(pending)
    return ordered, []


def pick_every(
    eligible: set[datetime.date], every: int, anchor: datetime.date
) -> set[datetime.date]:
    """Every Nth eligible date, counting from the first eligible date on or
    after the anchor. Nothing before the anchor: "starting Sep 22" means it.

    Strides over ELIGIBLE dates, not calendar days — "every other trash day"
    must survive a holiday shift with its phase intact.
    """
    if every <= 1:
        return {d for d in eligible if d >= anchor}
    run = sorted(d for d in eligible if d >= anchor)
    return {d for i, d in enumerate(run) if i % every == 0}


def pick_nth_of_month(
    eligible: set[datetime.date],
    nth: str,
    window: tuple[datetime.date, datetime.date],
) -> set[datetime.date]:
    """The Nth (1-5) or last eligible date of each month.

    Only months that lie ENTIRELY inside the computed window are considered:
    a month cut off by the window edge cannot say which date was truly first
    or last, and a wrong "first Monday" is worse than none.
    """
    by_month: dict[tuple[int, int], list[datetime.date]] = {}
    for d in eligible:
        by_month.setdefault((d.year, d.month), []).append(d)
    out: set[datetime.date] = set()
    lo, hi = window
    for (year, month), dates in by_month.items():
        first = datetime.date(year, month, 1)
        last = (first.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) \
            - datetime.timedelta(days=1)
        if first < lo or last > hi:
            continue
        dates.sort()
        if nth == PICK_NTH_LAST:
            out.add(dates[-1])
        else:
            idx = int(nth) - 1
            if idx < len(dates):
                out.add(dates[idx])
    return out


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
) -> set[datetime.date] | None:
    """Every date covered by any event on a calendar, over [start, end].

    Returns None when the calendar cannot be served yet (boot), so the caller
    can record it as missing and retry, rather than treating it as empty.

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
    except Exception as err:  # noqa: BLE001 - a missing/broken calendar must not kill setup
        # At boot an entity can HAVE a state a beat before its platform can
        # serve it: the state-change listener fires, we query, and HA answers
        # "did not match any entities". That is "not ready yet", not a fault.
        if "did not match any entities" in str(err):
            _LOGGER.debug("%s is not serviceable yet; will retry", entity_id)
            return None
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
    offset_days: int = 0
    # Derivation: build on another day-set, narrow to a cadence or ordinal,
    # keep only some months. All default to "off", which is the old model.
    base_day_set: str = ""
    pick: str = PICK_NONE
    pick_every: int = 1
    pick_anchor: str = ""
    pick_nth: str = "1"
    months: list[int] = field(default_factory=list)
    expose_calendar: bool = True

    _eligible: set[datetime.date] = field(default_factory=set, repr=False)
    _window: tuple[datetime.date, datetime.date] | None = field(
        default=None, repr=False
    )
    # Source calendars that did not exist at the last refresh (boot race).
    _missing_sources: list[str] = field(default_factory=list, repr=False)

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
            offset_days=int(config.get(CONF_OFFSET_DAYS) or 0),
            base_day_set=config.get(CONF_BASE_DAY_SET) or "",
            pick=config.get(CONF_PICK) or PICK_NONE,
            pick_every=int(config.get(CONF_PICK_EVERY) or 1),
            pick_anchor=config.get(CONF_PICK_ANCHOR) or "",
            pick_nth=str(config.get(CONF_PICK_NTH) or "1"),
            # The form stores months as strings ("11"); accept ints too.
            months=sorted({int(m) for m in (config.get(CONF_MONTHS) or [])}),
            expose_calendar=bool(config.get(CONF_EXPOSE_CALENDAR, True)),
        )

    @property
    def source_entities(self) -> list[str]:
        """Calendars this day-set reads, so we can re-evaluate when they change."""
        return [*self.base_calendars, *self.force_calendars, *self.exclude_calendars]

    @property
    def anchor_date(self) -> datetime.date | None:
        """The stride anchor as a date, or None when unset or unparseable."""
        if self.pick != PICK_EVERY or not self.pick_anchor:
            return None
        try:
            return datetime.date.fromisoformat(self.pick_anchor[:10])
        except ValueError:
            _LOGGER.error(
                "Day-set '%s' has an unreadable anchor %r; the stride is ignored",
                self.id, self.pick_anchor,
            )
            return None

    def describe_pick(self) -> str:
        """One line for the calendar entity's attributes and the card."""
        if self.pick == PICK_EVERY:
            return f"every {self.pick_every} from {self.pick_anchor or '?'}"
        if self.pick == PICK_NTH_OF_MONTH:
            n = self.pick_nth
            word = "last" if n == PICK_NTH_LAST else \
                {"1": "1st", "2": "2nd", "3": "3rd"}.get(n, f"{n}th")
            return f"{word} of the month"
        return ""

    async def async_refresh(
        self,
        hass: HomeAssistant,
        start: datetime.date,
        days: int = HORIZON_DAYS,
        base_set: set[datetime.date] | None = None,
    ) -> None:
        """Recompute eligibility across the whole window.

        `base_set` is the resolved eligibility of `base_day_set`, supplied by
        the registry, which evaluates day-sets in dependency order.
        """
        end = start + datetime.timedelta(days=days)

        # An offset moves dates in or out at the edges, so evaluate a window
        # widened by |offset| and clip back afterwards. Without this, a -1 day
        # set would be missing its first date and gain nothing at the end.
        pad = datetime.timedelta(days=abs(self.offset_days))
        calc_start, calc_end = start - pad, end + pad

        # A source calendar that does not exist YET is the normal case at boot:
        # this integration can set up before trash_day or workday have created
        # theirs, and `calendar.get_events` then raises "did not match any
        # entities". Skip it, remember it, and let the registry's source-change
        # listener re-run us the moment the entity appears. Not an error.
        missing: list[str] = []
        states = getattr(hass, "states", None)

        async def collect(entities, dates_spec, match=""):
            out: set[datetime.date] = set()
            for entity_id in entities:
                if states is not None and states.get(entity_id) is None:
                    missing.append(entity_id)
                    continue
                dates = await _calendar_dates(
                    hass, entity_id, calc_start, calc_end, match
                )
                if dates is None:            # present but not serviceable yet
                    missing.append(entity_id)
                    continue
                out |= dates
            out |= _dates_in_ranges(
                parse_date_spec(dates_spec), calc_start, calc_end
            )
            return out

        base = await collect(self.base_calendars, self.base_dates)
        if base_set:
            base |= base_set                     # built on another day-set
        forced = await collect(
            self.force_calendars, self.force_dates, self.force_match
        )
        vetoed = await collect(
            self.exclude_calendars, self.exclude_dates, self.exclude_match
        )

        mask = {WEEKDAYS.index(d) for d in self.weekdays if d in WEEKDAYS}

        # Stage 1: the three tiers decide which dates are eligible at all.
        hits: set[datetime.date] = set()
        cur = calc_start
        while cur <= calc_end:
            if cur in forced:
                hit = True                       # force wins outright
            elif cur in vetoed:
                hit = False                      # veto beats the base tier
            elif cur in base or (mask and cur.weekday() in mask):
                hit = True                       # base: calendars and/or mask
            else:
                hit = False
            if hit:
                hits.add(cur)
            cur += datetime.timedelta(days=1)

        # Stage 2: pick narrows to a cadence or an ordinal within each month.
        # Before invert, so "NOT the first Monday" means what it says.
        if self.pick == PICK_EVERY:
            anchor = self.anchor_date
            if anchor is not None:
                hits = pick_every(hits, self.pick_every, anchor)
        elif self.pick == PICK_NTH_OF_MONTH:
            hits = pick_nth_of_month(hits, self.pick_nth, (calc_start, calc_end))

        # Stage 3: month filter. After pick so a stride keeps its phase across
        # the year, and before offset so "day before the first Monday of
        # January" may legitimately land in December.
        if self.months:
            keep = set(self.months)
            hits = {d for d in hits if d.month in keep}

        eligible: set[datetime.date] = set()
        cur = calc_start
        while cur <= calc_end:
            if (cur in hits) != self.invert:
                eligible.add(cur)
            cur += datetime.timedelta(days=1)

        if self.offset_days:
            shift = datetime.timedelta(days=self.offset_days)
            eligible = {d + shift for d in eligible}
        # Clip back to the window the caller asked about.
        self._eligible = {d for d in eligible if start <= d <= end}
        self._window = (start, end)
        self._missing_sources = missing
        if missing:
            _LOGGER.debug(
                "Day-set '%s' computed without %s (not created yet); will "
                "recompute when it appears", self.id, missing,
            )

    def is_eligible(self, day: datetime.date) -> bool:
        """Whether `day` is in this set. False outside the computed window."""
        if self._window and day > self._window[1]:
            # Past the horizon is worth saying: it means a real limit was hit.
            _LOGGER.warning(
                "Day-set '%s' asked about %s, beyond its %s horizon",
                self.id, day, self._window[1],
            )
            return False
        if self._window and day < self._window[0]:
            # Before the window is ordinary — a calendar view of an old month.
            # Not an error, and not worth a log line per rendered cell.
            _LOGGER.debug(
                "Day-set '%s' asked about %s, before its %s window start",
                self.id, day, self._window[0],
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
        if self._missing_sources:
            # Empty because a source has not been created yet, not because the
            # horizon is genuinely bare. Say so at a level that does not read
            # like an outage — the listener will fill it in.
            _LOGGER.warning(
                "Day-set '%s' has no dates yet: waiting for %s to be created",
                self.id, self._missing_sources,
            )
            return None
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
