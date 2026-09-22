"""Offline tests for day-set evaluation.

Home Assistant is not installed on the dev box, and the logic worth testing --
precedence, multi-day coverage, invert, long gaps -- is pure. So we stub the
handful of HA surfaces day_set.py touches and run the real code.

    python3 tests/test_day_set.py
"""

import asyncio
import datetime
import sys
import types
from pathlib import Path

# --- stub just enough homeassistant ----------------------------------------
ha = types.ModuleType("homeassistant")
core = types.ModuleType("homeassistant.core")
core.HomeAssistant = object
util = types.ModuleType("homeassistant.util")
dt_util = types.ModuleType("homeassistant.util.dt")


def _start_of_local_day(d):
    return datetime.datetime(d.year, d.month, d.day)


dt_util.start_of_local_day = _start_of_local_day
dt_util.as_local = lambda v: v
dt_util.parse_datetime = lambda v: None
util.dt = dt_util
sys.modules.update({
    "homeassistant": ha,
    "homeassistant.core": core,
    "homeassistant.util": util,
    "homeassistant.util.dt": dt_util,
})

# Load day_set.py directly: importing the package would pull in __init__.py,
# which needs voluptuous and the rest of HA.
import importlib.util  # noqa: E402

_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "sb_scheduler"

# A stub package so day_set.py's `from .const import ...` resolves, without
# executing the real __init__.py.
_pkg = types.ModuleType("_sbpkg")
_pkg.__path__ = [str(_PKG)]
sys.modules["_sbpkg"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_sbpkg.{name}", _PKG / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"_sbpkg.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


_load("const")
_day_set = _load("day_set")

DaySet = _day_set.DaySet
InvalidDateSpec = _day_set.InvalidDateSpec
parse_date_spec = _day_set.parse_date_spec

D = datetime.date.fromisoformat
FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label}")


class FakeHass:
    """Returns canned calendar events for calendar.get_events."""

    def __init__(self, events_by_entity):
        self._events = events_by_entity
        self.services = self

    async def async_call(self, domain, service, data, blocking=False, return_response=False):
        entity_id = data["entity_id"]
        return {entity_id: {"events": self._events.get(entity_id, [])}}


def allday(start, end, summary="x"):
    """An all-day event: end is the morning after, as HA emits it."""
    return {"start": start, "end": end, "summary": summary}


# --- parsing ---------------------------------------------------------------
print("\nparse_date_spec")
check("single", parse_date_spec("2026-12-25"), [(D("2026-12-25"), D("2026-12-25"))])
check(
    "range",
    parse_date_spec("2027-06-05..2027-08-17"),
    [(D("2027-06-05"), D("2027-08-17"))],
)
check("empty", parse_date_spec("  "), [])
check(
    "mixed separators",
    len(parse_date_spec("2026-10-09,\n2027-02-12, 2026-12-22..2027-01-05")),
    3,
)
try:
    parse_date_spec("not-a-date")
    check("rejects garbage", "no raise", "InvalidDateSpec")
except InvalidDateSpec:
    check("rejects garbage", "raised", "raised")
try:
    parse_date_spec("2027-08-17..2027-06-05")
    check("rejects reversed range", "no raise", "InvalidDateSpec")
except InvalidDateSpec:
    check("rejects reversed range", "raised", "raised")


# --- school day: weekdays minus inline breaks ------------------------------
print("\nschool_day = Mon-Fri minus summer minus institute days")
school = DaySet(
    id="school_day",
    name="School Day",
    weekdays=["mon", "tue", "wed", "thu", "fri"],
    exclude_dates="2026-12-22..2027-01-05, 2027-06-05..2027-08-17, 2026-10-09",
)
asyncio.run(school.async_refresh(FakeHass({}), D("2026-09-01"), days=400))

check("Wed 2026-09-02 in session", school.is_eligible(D("2026-09-02")), True)
check("Sat 2026-09-05 weekend", school.is_eligible(D("2026-09-05")), False)
check("Fri 2026-10-09 institute day", school.is_eligible(D("2026-10-09")), False)
check("Thu 2026-12-24 winter break", school.is_eligible(D("2026-12-24")), False)
check("Mon 2027-01-04 still break", school.is_eligible(D("2027-01-04")), False)
check("Tue 2027-01-06 back", school.is_eligible(D("2027-01-06")), True)
check("Mon 2027-07-05 summer", school.is_eligible(D("2027-07-05")), False)

# THE case upstream's 16-day ceiling could not survive.
check(
    "next school day from mid-summer clears a 10-week gap",
    school.next_date_on_or_after(D("2027-06-10")),
    D("2027-08-18"),
)
check(
    "next school day skips the institute day",
    school.next_date_on_or_after(D("2026-10-09")),
    D("2026-10-12"),
)


# --- workday from a calendar, and its inverse ------------------------------
print("\nworkday from a calendar; non_workday = invert")
# Mon-Wed workdays, Thu is Thanksgiving (absent), Fri workday.
workday_events = [
    allday("2026-11-23", "2026-11-24"),
    allday("2026-11-24", "2026-11-25"),
    allday("2026-11-25", "2026-11-26"),
    allday("2026-11-27", "2026-11-28"),
]
hass = FakeHass({"calendar.workday": workday_events})

workday = DaySet(id="workday", name="Workday", base_calendars=["calendar.workday"])
non_workday = DaySet(
    id="non_workday", name="Non-workday",
    base_calendars=["calendar.workday"], invert=True,
)
asyncio.run(workday.async_refresh(hass, D("2026-11-23"), days=10))
asyncio.run(non_workday.async_refresh(hass, D("2026-11-23"), days=10))

check("Mon is a workday", workday.is_eligible(D("2026-11-23")), True)
check("Thanksgiving is not", workday.is_eligible(D("2026-11-26")), False)
check("Thanksgiving IS a non-workday", non_workday.is_eligible(D("2026-11-26")), True)
check("Sat is a non-workday", non_workday.is_eligible(D("2026-11-28")), True)
check("Mon is not a non-workday", non_workday.is_eligible(D("2026-11-23")), False)


# --- multi-day events cover the whole span ---------------------------------
print("\nmulti-day event covers [start, end)")
summer = FakeHass({"calendar.breaks": [allday("2027-06-05", "2027-08-18", "Summer")]})
spanned = DaySet(
    id="t", name="T",
    weekdays=["mon", "tue", "wed", "thu", "fri"],
    exclude_calendars=["calendar.breaks"],
)
asyncio.run(spanned.async_refresh(summer, D("2027-06-01"), days=120))
check("day before break", spanned.is_eligible(D("2027-06-04")), True)
check("mid-break Wednesday", spanned.is_eligible(D("2027-07-07")), False)
check("last break day", spanned.is_eligible(D("2027-08-17")), False)
check("day after break", spanned.is_eligible(D("2027-08-18")), True)


# --- precedence: force > veto > base ---------------------------------------
print("\nprecedence: force > veto > base")
both = FakeHass({
    "calendar.off": [allday("2026-11-26", "2026-11-27", "Day off")],
    "calendar.on": [allday("2026-11-26", "2026-11-27", "Workday")],
})
forced = DaySet(
    id="p", name="P",
    weekdays=["mon", "tue", "wed", "thu", "fri"],
    exclude_calendars=["calendar.off"],
    force_calendars=["calendar.on"],
)
asyncio.run(forced.async_refresh(both, D("2026-11-23"), days=10))
check("force overrides the veto", forced.is_eligible(D("2026-11-26")), True)

vetoed = DaySet(
    id="v", name="V",
    weekdays=["mon", "tue", "wed", "thu", "fri"],
    exclude_calendars=["calendar.off"],
)
asyncio.run(vetoed.async_refresh(both, D("2026-11-23"), days=10))
check("veto alone still wins over the mask", vetoed.is_eligible(D("2026-11-26")), False)

# An include source makes a date eligible even against the weekday mask --
# this is what lets Trash Day work with no mask at all.
saturday = FakeHass({"calendar.on": [allday("2026-11-28", "2026-11-29")]})
satset = DaySet(id="s", name="S", base_calendars=["calendar.on"])
asyncio.run(satset.async_refresh(saturday, D("2026-11-23"), days=10))
check("mask-less base-calendar set", satset.is_eligible(D("2026-11-28")), True)

# THE regression this tier split exists to fix: a base calendar MUST be
# vetoable. Under the old two-tier model the workday calendar was an
# `include`, which outranked `exclude`, so PTO was silently ignored.
print("\nregression: a base calendar can be vetoed (days_off)")
work = FakeHass({
    "calendar.workday": [allday("2026-11-26", "2026-11-28"),
                         allday("2026-12-24", "2026-12-25")],
    "calendar.days_off": [allday("2026-11-27", "2026-11-28", "Day after Thanksgiving"),
                          allday("2026-12-24", "2026-12-25", "Day off")],
})
workday = DaySet(
    id="workday", name="Workday",
    base_calendars=["calendar.workday"],
    exclude_calendars=["calendar.days_off"],
)
asyncio.run(workday.async_refresh(work, D("2026-11-20"), days=60))
check("a normal workday still counts", workday.is_eligible(D("2026-11-26")), True)
check("PTO cancels a workday", workday.is_eligible(D("2026-11-27")), False)
check("and again at Christmas Eve", workday.is_eligible(D("2026-12-24")), False)

# The "Workday"-titled override, which needs the title filter.
print("\nforce_match: a days-off entry titled 'Workday' reinstates one")
override = FakeHass({
    "calendar.workday": [],
    "calendar.days_off": [allday("2026-11-27", "2026-11-28", "Day after Thanksgiving"),
                          allday("2026-11-30", "2026-12-01", "Workday")],
})
ds = DaySet(
    id="w", name="W",
    weekdays=["mon", "tue", "wed", "thu", "fri"],
    exclude_calendars=["calendar.days_off"],
    force_calendars=["calendar.days_off"],
    force_match="Workday",
)
asyncio.run(ds.async_refresh(override, D("2026-11-23"), days=20))
check("plain day-off is vetoed", ds.is_eligible(D("2026-11-27")), False)
check("one titled 'Workday' is reinstated", ds.is_eligible(D("2026-11-30")), True)

print("\nexclude_match filters by title/description")
mixed = FakeHass({"calendar.hol": [
    dict(allday("2026-11-26", "2026-11-27", "Thanksgiving Day"), description="Public holiday"),
    dict(allday("2026-11-27", "2026-11-28", "Black Friday"), description="Observance"),
]})
pub = DaySet(
    id="h", name="H", weekdays=["mon", "tue", "wed", "thu", "fri"],
    exclude_calendars=["calendar.hol"], exclude_match="Public holiday",
)
asyncio.run(pub.async_refresh(mixed, D("2026-11-23"), days=10))
check("a public holiday is excluded", pub.is_eligible(D("2026-11-26")), False)
check("an observance is NOT", pub.is_eligible(D("2026-11-27")), True)

print("\nmigration: legacy include_* is read as base")
legacy = DaySet.from_config({
    "id": "l", "name": "L", "include_calendars": ["calendar.on"],
    "include_dates": "2026-12-25",
})
check("include_calendars -> base_calendars", legacy.base_calendars, ["calendar.on"])
check("include_dates -> base_dates", legacy.base_dates, "2026-12-25")
check("force stays empty", legacy.force_calendars, [])


# --- runs() merging --------------------------------------------------------
print("\nruns() merges consecutive dates")
runs = school.runs(D("2026-09-07"), D("2026-09-20"))
check("two school weeks -> two runs", len(runs), 2)
check("first run Mon-Fri", runs[0], (D("2026-09-07"), D("2026-09-11")))


# --- offset: "the night before trash day" ----------------------------------
print("\noffset_days shifts the whole set")
trash = FakeHass({"calendar.trash": [
    allday("2026-10-02", "2026-10-03", "Trash Day"),
    allday("2026-10-09", "2026-10-10", "Trash Day"),
    allday("2026-11-28", "2026-11-29", "Trash Day (moved)"),
]})
day_of = DaySet(id="t", name="Trash Day", base_calendars=["calendar.trash"])
eve = DaySet(id="te", name="Trash Day Eve",
             base_calendars=["calendar.trash"], offset_days=-1)
asyncio.run(day_of.async_refresh(trash, D("2026-10-01"), days=90))
asyncio.run(eve.async_refresh(trash, D("2026-10-01"), days=90))

check("collection day", day_of.is_eligible(D("2026-10-02")), True)
check("eve is the day before", eve.is_eligible(D("2026-10-01")), True)
check("eve is NOT the collection day", eve.is_eligible(D("2026-10-02")), False)
check("eve follows a holiday shift too",
      eve.is_eligible(D("2026-11-27")), True)
check("...and not the unshifted Friday", eve.is_eligible(D("2026-11-26")), False)

# The window must be widened, or the first date is lost at the edge.
edge = DaySet(id="e", name="E", base_calendars=["calendar.trash"], offset_days=-1)
asyncio.run(edge.async_refresh(trash, D("2026-10-01"), days=90))
check("a date shifting IN at the window start is not lost",
      edge.is_eligible(D("2026-10-01")), True)

fwd = DaySet(id="f", name="F", base_calendars=["calendar.trash"], offset_days=2)
asyncio.run(fwd.async_refresh(trash, D("2026-10-01"), days=90))
check("a positive offset moves forward", fwd.is_eligible(D("2026-10-04")), True)
check("zero offset is unchanged", day_of.is_eligible(D("2026-10-09")), True)

check("next_date_on_or_after respects the offset",
      eve.next_date_on_or_after(D("2026-10-03")), D("2026-10-08"))

# Offset applies AFTER invert, so it is always "that set, moved".
inv = DaySet(id="i", name="I", base_calendars=["calendar.trash"],
             invert=True, offset_days=-1)
asyncio.run(inv.async_refresh(trash, D("2026-10-01"), days=30))
check("inverted+offset: day before a NON-trash day",
      inv.is_eligible(D("2026-10-01")), False)
check("inverted+offset: 10-02 is the day before non-trash 10-03",
      inv.is_eligible(D("2026-10-02")), True)

# --- derivation: pick, months, base_day_set --------------------------------
order_by_dependency = _day_set.order_by_dependency
nohass = FakeHass({})
ALL = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MF = ["mon", "tue", "wed", "thu", "fri"]

print("\norder_by_dependency")
o, bad = order_by_dependency([
    {"id": "election", "base_day_set": "monday"},
    {"id": "monday"},
    {"id": "daily"},
])
check("base comes before its user", [c["id"] for c in o].index("monday")
      < [c["id"] for c in o].index("election"), True)
check("no unresolved", bad, [])
o, bad = order_by_dependency([{"id": "a", "base_day_set": "b"}, {"id": "b", "base_day_set": "a"}])
check("a cycle is reported, not hung", sorted(c["id"] for c in bad), ["a", "b"])
o, bad = order_by_dependency([{"id": "x", "base_day_set": "ghost"}])
check("a missing base is reported", [c["id"] for c in bad], ["x"])

print("\npick=every: every Nth ELIGIBLE date from an anchor")
e3 = DaySet(id="e3", name="Every 3rd day", weekdays=ALL,
            pick="every", pick_every=3, pick_anchor="2026-09-22")
asyncio.run(e3.async_refresh(nohass, D("2026-09-01"), days=60))
check("anchor day fires", e3.is_eligible(D("2026-09-22")), True)
check("next day does not", e3.is_eligible(D("2026-09-23")), False)
check("third day fires", e3.is_eligible(D("2026-09-25")), True)
check("sixth day fires", e3.is_eligible(D("2026-09-28")), True)
check("nothing BEFORE the anchor", e3.is_eligible(D("2026-09-21")), False)

# "Every other Tuesday" with NO Tuesday day-set: 14 days on Daily.
eot = DaySet(id="eot", name="Every other Tuesday", weekdays=ALL,
             pick="every", pick_every=14, pick_anchor="2026-09-22")
asyncio.run(eot.async_refresh(nohass, D("2026-09-01"), days=60))
check("Tue Sep 22", eot.is_eligible(D("2026-09-22")), True)
check("Tue Sep 29 skipped", eot.is_eligible(D("2026-09-29")), False)
check("Tue Oct 6", eot.is_eligible(D("2026-10-06")), True)

# Anchor that is NOT itself eligible snaps forward to the first that is.
snap = DaySet(id="snap", name="S", weekdays=["tue"],
              pick="every", pick_every=2, pick_anchor="2026-09-21")   # a Monday
asyncio.run(snap.async_refresh(nohass, D("2026-09-01"), days=60))
check("anchor on a Monday snaps to Tuesday", snap.is_eligible(D("2026-09-22")), True)
check("then every other", snap.is_eligible(D("2026-09-29")), False)
check("...", snap.is_eligible(D("2026-10-06")), True)

# The reason to stride over eligible dates: a holiday shift keeps phase.
trash2 = FakeHass({"calendar.trash": [
    allday("2026-11-13", "2026-11-14"), allday("2026-11-20", "2026-11-21"),
    allday("2026-11-28", "2026-11-29", "Trash Day (moved)"),   # Fri -> Sat
    allday("2026-12-04", "2026-12-05"),
]})
eotrash = DaySet(id="et", name="Every other trash day", base_calendars=["calendar.trash"],
                 pick="every", pick_every=2, pick_anchor="2026-11-13")
asyncio.run(eotrash.async_refresh(trash2, D("2026-11-01"), days=60))
check("1st collection", eotrash.is_eligible(D("2026-11-13")), True)
check("2nd skipped", eotrash.is_eligible(D("2026-11-20")), False)
check("3rd fires even though it MOVED to Saturday", eotrash.is_eligible(D("2026-11-28")), True)
check("4th skipped", eotrash.is_eligible(D("2026-12-04")), False)

print("\npick=nth_of_month")
tue2 = DaySet(id="t2", name="2nd Tuesday", weekdays=["tue"], pick="nth_of_month", pick_nth="2")
asyncio.run(tue2.async_refresh(nohass, D("2026-09-01"), days=90))
check("2nd Tue of Sep 2026 = Sep 8", tue2.is_eligible(D("2026-09-08")), True)
check("1st Tue is not", tue2.is_eligible(D("2026-09-01")), False)
check("2nd Tue of Oct 2026 = Oct 13", tue2.is_eligible(D("2026-10-13")), True)

lastwd = DaySet(id="lw", name="Last weekday", weekdays=MF, pick="nth_of_month", pick_nth="last")
asyncio.run(lastwd.async_refresh(nohass, D("2026-09-01"), days=90))
check("last weekday of Sep 2026 = Wed Sep 30", lastwd.is_eligible(D("2026-09-30")), True)
check("last weekday of Oct 2026 = Fri Oct 30 (31st is Sat)", lastwd.is_eligible(D("2026-10-30")), True)
check("Oct 29 is not", lastwd.is_eligible(D("2026-10-29")), False)

# A month cut by the window edge must NOT yield a wrong "first".
part = DaySet(id="p", name="1st Tue", weekdays=["tue"], pick="nth_of_month", pick_nth="1")
asyncio.run(part.async_refresh(nohass, D("2026-09-15"), days=60))
check("partial Sep yields no pick (Sep 1 is outside the window)",
      any(part.is_eligible(D(f"2026-09-{d:02d}")) for d in range(15, 31)), False)
check("full Oct picks Oct 6", part.is_eligible(D("2026-10-06")), True)

print("\nmonths filter")
novmon = DaySet(id="nm", name="Nov Mondays", weekdays=["mon"], months=[11])
asyncio.run(novmon.async_refresh(nohass, D("2026-10-01"), days=90))
check("a November Monday", novmon.is_eligible(D("2026-11-02")), True)
check("an October Monday is filtered", novmon.is_eligible(D("2026-10-05")), False)
check("months accepts strings from the form",
      DaySet.from_config({"id": "m", "name": "M", "months": ["11", "1"]}).months, [1, 11])

print("\nbase_day_set: composition via a resolved base set")
monday = DaySet(id="monday", name="Monday", weekdays=["mon"])
asyncio.run(monday.async_refresh(nohass, D("2026-01-01"), days=1100))
derived = DaySet(id="d", name="D", base_day_set="monday", pick="nth_of_month", pick_nth="1")
asyncio.run(derived.async_refresh(nohass, D("2026-01-01"), days=1100, base_set=monday._eligible))
check("first Monday of Sep 2026 = Sep 7", derived.is_eligible(D("2026-09-07")), True)
check("Sep 14 is not", derived.is_eligible(D("2026-09-14")), False)

# THE acceptance test: Election Day = day after the first Monday of November.
print("\nacceptance: Election Day = Monday -> 1st of month -> Nov -> +1")
election = DaySet(id="election_day", name="Election Day", base_day_set="monday",
                  pick="nth_of_month", pick_nth="1", months=[11], offset_days=1,
                  expose_calendar=False)
asyncio.run(election.async_refresh(nohass, D("2026-01-01"), days=1100, base_set=monday._eligible))
check("2026: Tue Nov 3", election.is_eligible(D("2026-11-03")), True)
check("2027: Tue Nov 2 (Nov 1 is a Monday)", election.is_eligible(D("2027-11-02")), True)
check("2028: Tue Nov 7", election.is_eligible(D("2028-11-07")), True)
check("not the Monday itself", election.is_eligible(D("2026-11-02")), False)
check("not the second Tuesday", election.is_eligible(D("2026-11-10")), False)
check("nothing outside November",
      any(election.is_eligible(D(f"2026-{m:02d}-01") + datetime.timedelta(days=k))
          for m in (1, 4, 7, 10) for k in range(28)), False)
check("exactly one date per year",
      sum(1 for k in range(366) if election.is_eligible(D("2027-01-01") + datetime.timedelta(days=k))), 1)
check("next from Sep 21 2026 is Nov 3", election.next_date_on_or_after(D("2026-09-21")), D("2026-11-03"))
check("calendar opt-out is read", election.expose_calendar, False)
check("default keeps the calendar", monday.expose_calendar, True)
check("describe_pick", election.describe_pick(), "1st of the month")

print("\nboot race: a source calendar that does not exist yet")


class LateHass(FakeHass):
    """Like FakeHass, but with a `states` registry that knows only some
    entities — the shape of HA during startup, before trash_day has created
    its calendar. get_events on an unknown entity would raise in real HA."""

    def __init__(self, events_by_entity, existing):
        super().__init__(events_by_entity)
        self.states = self
        self._existing = set(existing)
        self.calls = []

    def get(self, entity_id):
        return object() if entity_id in self._existing else None

    async def async_call(self, domain, service, data, blocking=False, return_response=False):
        self.calls.append(data["entity_id"])
        if data["entity_id"] not in self._existing:
            raise RuntimeError("Service call requested response data but did not match any entities")
        return await super().async_call(domain, service, data, blocking, return_response)


late = LateHass({"calendar.trash": [allday("2026-10-02", "2026-10-03")]}, existing=[])
early = DaySet(id="t", name="Trash Day", base_calendars=["calendar.trash"])
asyncio.run(early.async_refresh(late, D("2026-10-01"), days=30))
check("a missing source is skipped, not called", late.calls, [])
check("...and remembered", early._missing_sources, ["calendar.trash"])
check("the set is empty for now", early.is_eligible(D("2026-10-02")), False)
check("next date is None without raising", early.next_date_on_or_after(D("2026-10-01")), None)

late._existing.add("calendar.trash")          # the entity appears
asyncio.run(early.async_refresh(late, D("2026-10-01"), days=30))
check("once present it is read", late.calls, ["calendar.trash"])
check("...and the set fills in", early.is_eligible(D("2026-10-02")), True)
check("nothing is missing any more", early._missing_sources, [])

# The second boot shape: the entity HAS a state, but its platform cannot serve
# it yet, so get_events raises "did not match any entities". That is what the
# first fix missed — it must count as missing, not as empty-with-a-traceback.


class HalfUpHass(LateHass):
    def get(self, entity_id):
        return object()                         # state exists…

    async def async_call(self, domain, service, data, blocking=False, return_response=False):
        self.calls.append(data["entity_id"])
        if data["entity_id"] not in self._existing:   # …but the service can't see it
            raise RuntimeError("Service call requested response data but did not match any entities")
        return await FakeHass.async_call(self, domain, service, data, blocking, return_response)


half = HalfUpHass({"calendar.trash": [allday("2026-10-02", "2026-10-03")]}, existing=[])
mid = DaySet(id="t", name="Trash Day", base_calendars=["calendar.trash"])
asyncio.run(mid.async_refresh(half, D("2026-10-01"), days=30))
check("a present-but-unserviceable source is attempted", half.calls, ["calendar.trash"])
check("...and counted as missing, not empty", mid._missing_sources, ["calendar.trash"])
check("no date yet, no exception", mid.next_date_on_or_after(D("2026-10-01")), None)
half._existing.add("calendar.trash")
asyncio.run(mid.async_refresh(half, D("2026-10-01"), days=30))
check("the retry fills it in", mid.is_eligible(D("2026-10-02")), True)
check("...and clears missing", mid._missing_sources, [])

# The third shape (seen 2026-09-21 on calendar.anderson): a Google calendar is
# up and serviceable, but its first sync is still running. Reads as empty if
# treated as a fault — and a busy calendar reading as empty hides a day off.
class SyncingHass(HalfUpHass):
    async def async_call(self, domain, service, data, blocking=False, return_response=False):
        self.calls.append(data["entity_id"])
        if data["entity_id"] not in self._existing:
            raise RuntimeError("Unable to get events: Sync from server has not completed")
        return await FakeHass.async_call(self, domain, service, data, blocking, return_response)


syncing = SyncingHass({"calendar.g": [allday("2026-10-02", "2026-10-03")]}, existing=[])
gs = DaySet(id="g", name="G", base_calendars=["calendar.g"])
asyncio.run(gs.async_refresh(syncing, D("2026-10-01"), days=30))
check("a still-syncing Google calendar counts as missing", gs._missing_sources, ["calendar.g"])
syncing._existing.add("calendar.g")
asyncio.run(gs.async_refresh(syncing, D("2026-10-01"), days=30))
check("...and the retry fills it in", gs.is_eligible(D("2026-10-02")), True)

# A genuinely broken calendar is still an error, not "missing".
class BrokenHass(LateHass):
    def get(self, entity_id): return object()
    async def async_call(self, *a, **k): raise RuntimeError("boom")
broken = DaySet(id="b", name="B", base_calendars=["calendar.x"])
asyncio.run(broken.async_refresh(BrokenHass({}, existing=["calendar.x"]), D("2026-10-01"), days=30))
check("an unrelated failure is NOT treated as missing", broken._missing_sources, [])

print("\nvalidate_day_set — shared by the form and the set_day_set service")
validate = _day_set.validate_day_set
existing = [{"id": "monday", "name": "Monday"},
            {"id": "election_day", "name": "Election Day", "base_day_set": "monday"}]
check("a plain valid set", validate({"name": "X", "weekdays": ["tue"]}, existing, None), {})
check("name required", validate({"name": "  "}, existing, None).get("base"), "name_required")
check("bad dates are flagged by field",
      validate({"name": "X", "base_dates": "nope"}, existing, None).get("base_dates"), "invalid_dates")
check("every needs an anchor",
      validate({"name": "X", "pick": "every"}, existing, None).get("base"), "anchor_required")
check("every with an anchor is fine",
      validate({"name": "X", "pick": "every", "pick_anchor": "2026-09-22"}, existing, None), {})
check("building on a missing set",
      validate({"name": "X", "base_day_set": "ghost"}, existing, None).get("base"), "base_missing")
check("building on yourself",
      validate({"name": "Monday", "base_day_set": "monday"}, existing, "monday").get("base"), "base_cycle")
check("an indirect cycle (monday -> election_day -> monday)",
      validate({"name": "Monday", "base_day_set": "election_day"}, existing, "monday").get("base"), "base_cycle")
check("a legitimate chain is allowed",
      validate({"name": "Y", "base_day_set": "election_day"}, existing, None), {})
check("every code has a message",
      all(c in _day_set.VALIDATION_MESSAGES for c in
          ("invalid_dates", "anchor_required", "base_cycle", "base_missing", "name_required")), True)

print("\nallocate_day_set_id")
slug = lambda s: "".join(ch if ch.isalnum() else "_" for ch in s.lower()).strip("_")
alloc = _day_set.allocate_day_set_id
check("slug of the name", alloc("School Day", set(), slug), "school_day")
check("collision gets _2", alloc("School Day", {"school_day"}, slug), "school_day_2")
check("...then _3", alloc("School Day", {"school_day", "school_day_2"}, slug), "school_day_3")
check("empty name still yields an id", alloc("", set(), slug), "day_set")

print("\nNegatedDaySet — a schedule's `negate` flag, the complement of its day-set")
NegatedDaySet = _day_set.NegatedDaySet
wk = DaySet(id="workday", name="Workday", weekdays=MF)
asyncio.run(wk.async_refresh(nohass, D("2026-09-14"), days=30))
nwk = NegatedDaySet(wk)
check("Mon Sep 21 is NOT a non-workday", nwk.is_eligible(D("2026-09-21")), False)
check("Sat Sep 26 IS", nwk.is_eligible(D("2026-09-26")), True)
check("next non-workday from Mon Sep 21 = Sat Sep 26", nwk.next_date_on_or_after(D("2026-09-21")), D("2026-09-26"))
check("next from Sat itself = Sat", nwk.next_date_on_or_after(D("2026-09-26")), D("2026-09-26"))
check("outside the window is not eligible", nwk.is_eligible(D("2030-01-01")), False)
check("name reads as the complement", (nwk.id, nwk.name), ("not workday", "Not Workday"))
every = DaySet(id="daily", name="Daily", weekdays=ALL)
asyncio.run(every.async_refresh(nohass, D("2026-09-14"), days=30))
check("negating Daily yields nothing (and logs, not raises)",
      NegatedDaySet(every).next_date_on_or_after(D("2026-09-21")), None)
none = DaySet(id="never", name="Never")
asyncio.run(none.async_refresh(nohass, D("2026-09-14"), days=30))
check("negating an empty set is every day", NegatedDaySet(none).next_date_on_or_after(D("2026-09-21")), D("2026-09-21"))

print("\nper-calendar match rules: 'calendar.x: needle' in a tier's match text")
parse_match_spec = _day_set.parse_match_spec
match_for = _day_set.match_for
check("bare text is the default for all", parse_match_spec("Workday"), ("Workday", {}))
check("a rule line is per calendar", parse_match_spec("calendar.anderson: #do"), ("", {"calendar.anderson": "#do"}))
check("both, with ; as separator too",
      parse_match_spec("Workday; calendar.anderson: #wd\ncalendar.other: tag"),
      ("Workday", {"calendar.anderson": "#wd", "calendar.other": "tag"}))
check("lookup falls back to the default", match_for("calendar.anderson: #do", "calendar.days_off"), "")
check("lookup finds the specific rule", match_for("calendar.anderson: #do", "calendar.anderson"), "#do")

# THE case: days_off cancels on ANY entry, the personal Google calendar only on #do.
two = FakeHass({
    "calendar.workday": [allday(f"2026-09-{d:02d}", f"2026-09-{d+1:02d}") for d in (21, 22, 23, 24, 25)],
    "calendar.days_off": [allday("2026-09-22", "2026-09-23", "Dentist")],
    "calendar.anderson": [allday("2026-09-24", "2026-09-25", "Dogs - Boarding"),
                          allday("2026-09-25", "2026-09-26", "#do"),
                          dict(allday("2026-09-23", "2026-09-24", "Errand"), description="notes #do")],
})
wd = DaySet(id="w", name="Workday", base_calendars=["calendar.workday"],
            exclude_calendars=["calendar.days_off", "calendar.anderson"],
            exclude_match="calendar.anderson: #do")
asyncio.run(wd.async_refresh(two, D("2026-09-20"), days=10))
check("Mon: plain workday", wd.is_eligible(D("2026-09-21")), True)
check("Tue: days_off cancels on ANY entry (no #do needed)", wd.is_eligible(D("2026-09-22")), False)
check("Wed: '#do' in the DESCRIPTION cancels", wd.is_eligible(D("2026-09-23")), False)
check("Thu: an untagged Anderson entry does NOT cancel", wd.is_eligible(D("2026-09-24")), True)
check("Fri: an all-day event TITLED #do cancels", wd.is_eligible(D("2026-09-25")), False)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S)")
    sys.exit(1)
print("all day-set tests passed")
