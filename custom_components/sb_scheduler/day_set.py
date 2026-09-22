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
import re
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

# Substrings of a `calendar.get_events` failure that mean "come back later",
# not "broken". Matched on the message because HA raises plain
# HomeAssistantError / ServiceValidationError for all of them.
NOT_READY_MARKERS = (
    "did not match any entities",           # core: entity registered, platform not serving
    "Sync from server has not completed",   # google: first sync still in flight
)

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


# Every field a day-set config may carry. The services filter incoming data
# to this so a stray key can never land in the config entry.
DAY_SET_FIELDS = (
    CONF_NAME, CONF_WEEKDAYS, CONF_BASE_DAY_SET, CONF_BASE_CALENDARS, CONF_BASE_DATES,
    CONF_EXCLUDE_CALENDARS, CONF_EXCLUDE_DATES, CONF_EXCLUDE_MATCH,
    CONF_FORCE_CALENDARS, CONF_FORCE_DATES, CONF_FORCE_MATCH,
    CONF_PICK, CONF_PICK_EVERY, CONF_PICK_ANCHOR, CONF_PICK_NTH, CONF_MONTHS,
    CONF_INVERT, CONF_OFFSET_DAYS, CONF_EXPOSE_CALENDAR,
)

# One vocabulary for both the options form (codes) and the services (text).
VALIDATION_MESSAGES = {
    "invalid_dates": "Could not read those dates. Use YYYY-MM-DD, separated by "
                     "commas, with ranges written as YYYY-MM-DD..YYYY-MM-DD.",
    "anchor_required": '"Every Nth" needs a starting date.',
    "base_cycle": "That day-set is built on this one (directly or through "
                  "others), which would loop forever.",
    "base_missing": "That base day-set does not exist.",
    "name_required": "A day-set needs a name.",
}


def validate_day_set(flat: dict, day_sets: list[dict], editing: str | None) -> dict[str, str]:
    """What can be wrong with a day-set config, keyed by field ("base" =
    form-level). Shared by the options flow and the set_day_set service so the
    two entry points cannot disagree about what is allowed."""
    errors: dict[str, str] = {}
    if not str(flat.get(CONF_NAME) or "").strip():
        errors["base"] = "name_required"
    for key in (CONF_BASE_DATES, CONF_EXCLUDE_DATES, CONF_FORCE_DATES):
        try:
            parse_date_spec(flat.get(key) or "")
        except InvalidDateSpec:
            errors[key] = "invalid_dates"
            errors.setdefault("base", "invalid_dates")

    if flat.get(CONF_PICK) == PICK_EVERY and not flat.get(CONF_PICK_ANCHOR):
        errors["base"] = "anchor_required"

    base = flat.get(CONF_BASE_DAY_SET) or ""
    if base:
        by_id = {d[CONF_ID]: d for d in day_sets}
        seen: set[str] = set()
        cur = base
        while cur:
            if cur == editing or cur in seen:
                errors["base"] = "base_cycle"
                break
            cfg = by_id.get(cur)
            if cfg is None:
                errors["base"] = "base_missing"
                break
            seen.add(cur)
            cur = cfg.get(CONF_BASE_DAY_SET) or ""
    return errors


def allocate_day_set_id(name: str, taken: set[str], slug) -> str:
    """A readable id from the name: "school_day" beats "e94e9983", because
    schedules reference day-sets by id and it ends up in every service call.
    `slug` is HA's slugify, passed in so this stays importable offline."""
    base = slug(name) or "day_set"
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


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


MATCH_RULE = re.compile(r"^\s*(calendar\.[a-z0-9_]+)\s*:\s*(.*?)\s*$")


def parse_match_spec(raw: str) -> tuple[str, dict[str, str]]:
    """A tier's match text, as (default needle, per-calendar needles).

    A bare string applies to every calendar in the tier, as before. A line
    `calendar.anderson: #do` applies only to that calendar, so one tier can
    veto on ANY entry of a days-off calendar and on TAGGED entries of a busy
    personal one. Lines and `;` both separate rules.
    """
    default_parts: list[str] = []
    per: dict[str, str] = {}
    for part in re.split(r"[;\n]", raw or ""):
        m = MATCH_RULE.match(part)
        if m:
            per[m.group(1)] = m.group(2)
        elif part.strip():
            default_parts.append(part.strip())
    return " ".join(default_parts), per


def match_for(spec: str, entity_id: str) -> str:
    """The needle that applies to one calendar under a tier's match text."""
    default, per = parse_match_spec(spec)
    return per.get(entity_id, default)


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
        # A Google calendar has a third shape: entity up, service reachable,
        # but its first sync still in flight — "Sync from server has not
        # completed". Same treatment, or a busy personal calendar reads as
        # empty (and a day off is missed) until the next refresh.
        if any(s in str(err) for s in NOT_READY_MARKERS):
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
                    hass, entity_id, calc_start, calc_end, match_for(match, entity_id)
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


class NegatedDaySet:
    """The complement of a day-set, for a schedule with `negate` set.

    Answers the one question the timer asks — the next date on or after a
    day — with the first date inside the base set's computed window that the
    base does NOT contain. Reads the base's resolved set directly rather than
    going through `is_eligible`, whose "beyond the horizon" warning would
    otherwise fire for every probe past the window.
    """

    def __init__(self, base: DaySet) -> None:
        self.base = base
        self.id = f"not {base.id}"
        self.name = f"Not {base.name}"

    def is_eligible(self, day: datetime.date) -> bool:
        w = self.base._window
        if not w or day < w[0] or day > w[1]:
            return False
        return day not in self.base._eligible

    def next_date_on_or_after(self, day: datetime.date) -> datetime.date | None:
        w = self.base._window
        if not w:
            return None
        cur = max(day, w[0])
        while cur <= w[1]:
            if cur not in self.base._eligible:
                return cur
            cur += datetime.timedelta(days=1)
        _LOGGER.error(
            "Every date from %s to the horizon (%s) is in day-set '%s'; a schedule "
            "negating it will never run.",
            day, w[1], self.base.id,
        )
        return None
