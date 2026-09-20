"""Offline tests for next-trigger calculation.

timer.py has no Home Assistant imports at all -- deliberately, so the part that
replaces upstream's broken look-ahead can be tested without a running HA.

    python3 tests/test_timer.py
"""

import datetime
import importlib.util
import sys
import types
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "sb_scheduler"
_pkg = types.ModuleType("_sbpkg2")
_pkg.__path__ = [str(_PKG)]
sys.modules["_sbpkg2"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_sbpkg2.{name}", _PKG / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"_sbpkg2.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


_load("const")
timer = _load("timer")

D = datetime.date.fromisoformat
FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(label)
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label}")


class FakeDaySet:
    """Eligible on the given dates only."""

    def __init__(self, dates):
        self.dates = sorted(dates)

    def next_date_on_or_after(self, day):
        for d in self.dates:
            if d >= day:
                return d
        return None


def dt(s):
    return datetime.datetime.fromisoformat(s)


def times_of(pattern):
    """Times on an arbitrary fixed day, for patterns with no sun component."""
    return [m.time() for m in timer.times_on(pattern, D("2026-09-21"))]


def fake_sun(event, day):
    """Sunrise 06:40, sunset 19:00, shifting a minute a day so it is not static."""
    shift = datetime.timedelta(minutes=(day - D("2026-09-21")).days)
    base = datetime.time(6, 40) if event == "sunrise" else datetime.time(19, 0)
    return datetime.datetime.combine(day, base) + shift


# --- occurrence expansion --------------------------------------------------
print("\noccurrences")
check(
    "discrete times are sorted",
    times_of({"type": "occurrences", "occurrences": ["18:00", "06:30"]}),
    [datetime.time(6, 30), datetime.time(18, 0)],
)
check(
    "accepts H:MM and HH:MM:SS",
    times_of({"type": "occurrences", "occurrences": ["6:05", "07:00:00"]}),
    [datetime.time(6, 5), datetime.time(7, 0)],
)
check(
    "unreadable times are dropped, not fatal",
    times_of({"type": "occurrences", "occurrences": ["06:30", "banana", "25:00"]}),
    [datetime.time(6, 30)],
)

# The user's own example: every 15 min, 09:00 until 13:00.
interval = times_of(
    {"type": "interval", "start": "09:00", "stop": "13:00", "every_minutes": 15}
)
check("09:00-13:00 every 15m -> 17 firings", len(interval), 17)
check("first firing", interval[0], datetime.time(9, 0))
check("last firing", interval[-1], datetime.time(13, 0))
check(
    "interval with no step is refused, not guessed",
    times_of({"type": "interval", "start": "09:00", "stop": "13:00"}),
    [],
)
check(
    "runaway interval is capped",
    len(times_of(
        {"type": "interval", "start": "00:00", "stop": "23:59", "every_minutes": 1}
    )),
    288,
)


# --- next_trigger ----------------------------------------------------------
print("\nnext_trigger")
weekdays = FakeDaySet([D("2026-09-21"), D("2026-09-22"), D("2026-09-23")])
wake = {"name": "Wake", "pattern": {"type": "occurrences", "occurrences": ["06:30"]}}

check(
    "before the time today -> today",
    timer.next_trigger(wake, weekdays, dt("2026-09-21T05:00:00")),
    dt("2026-09-21T06:30:00"),
)
check(
    "after the time today -> next eligible day",
    timer.next_trigger(wake, weekdays, dt("2026-09-21T07:00:00")),
    dt("2026-09-22T06:30:00"),
)
check(
    "on a non-eligible day -> next eligible day",
    timer.next_trigger(wake, weekdays, dt("2026-09-20T05:00:00")),
    dt("2026-09-21T06:30:00"),
)
check(
    "exactly at the trigger time -> next one (strictly after now)",
    timer.next_trigger(wake, weekdays, dt("2026-09-21T06:30:00")),
    dt("2026-09-22T06:30:00"),
)

twice = {"name": "Twice", "pattern": {"type": "occurrences", "occurrences": ["06:30", "18:00"]}}
check(
    "picks the later slot the same day",
    timer.next_trigger(twice, weekdays, dt("2026-09-21T07:00:00")),
    dt("2026-09-21T18:00:00"),
)

# THE case: a school-year gap. Upstream's 16-iteration cap could not do this.
school = FakeDaySet([D("2027-06-03"), D("2027-08-18"), D("2027-08-19")])
check(
    "clears a 10-week gap",
    timer.next_trigger(wake, school, dt("2027-06-03T09:00:00")),
    dt("2027-08-18T06:30:00"),
)

check(
    "past the day-set horizon returns None rather than a wrong time",
    timer.next_trigger(wake, FakeDaySet([]), dt("2026-09-21T05:00:00")),
    None,
)
check(
    "no usable times returns None",
    timer.next_trigger(
        {"name": "Empty", "pattern": {"type": "occurrences", "occurrences": []}},
        weekdays, dt("2026-09-21T05:00:00")),
    None,
)

# Timezone-aware now must produce a timezone-aware trigger, or comparisons
# against HA's clock raise TypeError at runtime.
aware = datetime.datetime(2026, 9, 21, 5, 0, tzinfo=datetime.timezone.utc)
result = timer.next_trigger(wake, weekdays, aware)
check("tz-aware now -> tz-aware result", result.tzinfo, datetime.timezone.utc)

interval_sched = {
    "name": "Interval",
    "pattern": {"type": "interval", "start": "09:00", "stop": "13:00", "every_minutes": 15},
}
check(
    "interval mid-window picks the next step",
    timer.next_trigger(interval_sched, weekdays, dt("2026-09-21T09:07:00")),
    dt("2026-09-21T09:15:00"),
)
check(
    "interval after the window rolls to the next day",
    timer.next_trigger(interval_sched, weekdays, dt("2026-09-21T13:30:00")),
    dt("2026-09-22T09:00:00"),
)

# --- sun-relative occurrences ----------------------------------------------
print("\nsun-relative occurrences")
check("bare sunset parses", timer.parse_occurrence("sunset").event, "sunset")
check("offset parses", timer.parse_occurrence("sunset+00:15").offset,
      datetime.timedelta(minutes=15))
check("negative offset parses", timer.parse_occurrence("sunrise-01:30:00").offset,
      datetime.timedelta(hours=-1, minutes=-30))
check("seconds offset parses", timer.parse_occurrence("sunset+00:15:00").offset,
      datetime.timedelta(minutes=15))
check("garbage rejected", timer.parse_occurrence("moonrise+00:15"), None)
check("fixed time still parses", timer.parse_occurrence("06:30").fixed,
      datetime.time(6, 30))

sun_pattern = {"type": "occurrences", "occurrences": ["sunset+00:15:00"]}
check("resolves against the date, not 'next sunset'",
      timer.times_on(sun_pattern, D("2026-09-21"), fake_sun),
      [dt("2026-09-21T19:15:00")])
check("and moves with the date",
      timer.times_on(sun_pattern, D("2026-09-25"), fake_sun),
      [dt("2026-09-25T19:19:00")])

garden_on = {"name": "Garden lights on", "pattern": sun_pattern}
check("next trigger uses that day's sunset",
      timer.next_trigger(garden_on, weekdays, dt("2026-09-21T12:00:00"), fake_sun),
      dt("2026-09-21T19:15:00"))
check("after sunset rolls to the next eligible day's sunset",
      timer.next_trigger(garden_on, weekdays, dt("2026-09-21T20:00:00"), fake_sun),
      dt("2026-09-22T19:16:00"))

# Mixed fixed + sun must sort by resolved time, not by config order.
mixed = {"name": "Mixed", "pattern": {"type": "occurrences",
         "occurrences": ["sunset+00:15:00", "06:30", "sunrise-00:10:00"]}}
# sunrise-00:10 lands on 06:30 too, which is the point: ordering is by the
# RESOLVED moment, not by config order or by kind.
check("mixed occurrences sort by resolved moment",
      [m.time().isoformat("minutes") for m in
       timer.times_on(mixed["pattern"], D("2026-09-21"), fake_sun)],
      ["06:30", "06:30", "19:15"])

check("a sun occurrence with no resolver is skipped, not crashed",
      timer.times_on(sun_pattern, D("2026-09-21"), None), [])
check("polar night (resolver returns None) is survivable",
      timer.times_on(sun_pattern, D("2026-09-21"), lambda e, d: None), [])

# --- REGRESSION: mixing a fixed time with an AWARE sun time ----------------
# This is what broke "Garden Lights - On" live: sorting naive + aware raises
# TypeError, so the entity registered but never wrote a state. A single sun
# occurrence survives because a one-element sort never compares.
print("\nregression: naive fixed time + aware sun time")
TZ = datetime.timezone(datetime.timedelta(hours=-5))


def aware_sun(event, day):
    base = datetime.time(6, 40) if event == "sunrise" else datetime.time(19, 0)
    return datetime.datetime.combine(day, base, tzinfo=TZ)


mixed_tz = {"type": "occurrences", "occurrences": ["00:00", "sunset+00:15:00"]}
got = timer.times_on(mixed_tz, D("2026-09-21"), aware_sun)
check("mixed naive/aware sorts instead of raising", len(got), 2)
check("all results end up aware", all(m.tzinfo is not None for m in got), True)
check("midnight sorts first", got[0].hour, 0)
check(
    "next_trigger survives the mixed pattern",
    timer.next_trigger({"name": "Garden", "pattern": mixed_tz}, weekdays,
                       datetime.datetime(2026, 9, 21, 12, 0, tzinfo=TZ), aware_sun),
    datetime.datetime(2026, 9, 21, 19, 15, tzinfo=TZ),
)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S)")
    sys.exit(1)
print("all timer tests passed")
