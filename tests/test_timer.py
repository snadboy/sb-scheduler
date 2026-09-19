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


# --- occurrence expansion --------------------------------------------------
print("\noccurrence_times")
check(
    "discrete times are sorted",
    timer.occurrence_times({"type": "occurrences", "occurrences": ["18:00", "06:30"]}),
    [datetime.time(6, 30), datetime.time(18, 0)],
)
check(
    "accepts H:MM and HH:MM:SS",
    timer.occurrence_times({"type": "occurrences", "occurrences": ["6:05", "07:00:00"]}),
    [datetime.time(6, 5), datetime.time(7, 0)],
)
check(
    "unreadable times are dropped, not fatal",
    timer.occurrence_times({"type": "occurrences", "occurrences": ["06:30", "banana", "25:00"]}),
    [datetime.time(6, 30)],
)

# The user's own example: every 15 min, 09:00 until 13:00.
interval = timer.occurrence_times(
    {"type": "interval", "start": "09:00", "stop": "13:00", "every_minutes": 15}
)
check("09:00-13:00 every 15m -> 17 firings", len(interval), 17)
check("first firing", interval[0], datetime.time(9, 0))
check("last firing", interval[-1], datetime.time(13, 0))
check(
    "interval with no step is refused, not guessed",
    timer.occurrence_times({"type": "interval", "start": "09:00", "stop": "13:00"}),
    [],
)
check(
    "runaway interval is capped",
    len(timer.occurrence_times(
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

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S)")
    sys.exit(1)
print("all timer tests passed")
