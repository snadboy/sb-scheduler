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


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S)")
    sys.exit(1)
print("all day-set tests passed")
