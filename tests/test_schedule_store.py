"""Offline tests for the schedule storage contract.

The action shape matters more than it looks: the inherited action engine reads
`action["service_data"]` with an unconditional subscript, so a missing key is a
KeyError at FIRING time, hours or days after the schedule was created.

    python3 tests/test_schedule_store.py
"""

import importlib.util
import sys
import types
from pathlib import Path

ha = types.ModuleType("homeassistant")
core = types.ModuleType("homeassistant.core")
core.HomeAssistant = object
core.callback = lambda f: f
helpers = types.ModuleType("homeassistant.helpers")
storage = types.ModuleType("homeassistant.helpers.storage")
storage.Store = object
helpers.storage = storage
sys.modules.update({
    "homeassistant": ha, "homeassistant.core": core,
    "homeassistant.helpers": helpers, "homeassistant.helpers.storage": storage,
})

_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "sb_scheduler"
_pkg = types.ModuleType("_sbpkg3")
_pkg.__path__ = [str(_PKG)]
sys.modules["_sbpkg3"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_sbpkg3.{name}", _PKG / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"_sbpkg3.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


_load("const")
store = _load("schedule_store")

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(label)
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label}")


print("\nnormalise_action")
check("missing service_data becomes {}",
      store.normalise_action({"service": "switch.turn_on", "entity_id": "switch.x"}),
      {"service": "switch.turn_on", "entity_id": "switch.x", "service_data": {}})
check("existing service_data is kept",
      store.normalise_action({"service": "light.turn_on", "service_data": {"brightness": 3}}),
      {"service": "light.turn_on", "service_data": {"brightness": 3}})
check("`data` is accepted as an alias (scheduler-component's own key)",
      store.normalise_action({"service": "light.turn_on", "data": {"brightness": 3}}),
      {"service": "light.turn_on", "service_data": {"brightness": 3}})
check("service_data wins over data, and data is dropped",
      store.normalise_action({"service": "x", "service_data": {"a": 1}, "data": {"b": 2}}),
      {"service": "x", "service_data": {"a": 1}})

print("\nnormalise_schedule — steps")
one = store.normalise_schedule({
    "name": "Garden Irrigation", "day_set": "daily",
    "steps": [
        {"name": "On",  "pattern": {"type": "occurrences", "occurrences": ["06:00"]},
         "actions": [{"service": "switch.turn_on", "entity_id": "switch.x"}]},
        {"name": "Off", "pattern": {"type": "occurrences", "occurrences": ["07:00"]},
         "actions": [{"service": "switch.turn_off", "entity_id": "switch.x"}]},
    ]})
check("two steps kept", len(one["steps"]), 2)
check("step ids generated", [s["step_id"] for s in one["steps"]], ["s1", "s2"])
check("step names kept", [s["name"] for s in one["steps"]], ["On", "Off"])
check("each step normalises its own actions",
      one["steps"][0]["actions"][0].get("service_data"), {})
check("steps default to enabled", all(s["enabled"] for s in one["steps"]), True)
check("day_set is stated ONCE, on the schedule", one["day_set"], "daily")
check("no top-level pattern remains", "pattern" in one, False)

print("\nmigration — a pre-steps schedule becomes one implicit step")
legacy = store.normalise_schedule({
    "name": "Bedside Lamps", "day_set": "workday",
    "pattern": {"type": "occurrences", "occurrences": ["06:30"]},
    "actions": [{"service": "light.turn_on", "entity_id": "light.x",
                 "service_data": {"brightness": 3}}],
    "last_triggered": "2026-09-19T22:10:38-05:00",
})
check("exactly one step", len(legacy["steps"]), 1)
check("its pattern came from the top level",
      legacy["steps"][0]["pattern"]["occurrences"], ["06:30"])
check("its actions came from the top level",
      legacy["steps"][0]["actions"][0]["service"], "light.turn_on")
check("service_data preserved",
      legacy["steps"][0]["actions"][0]["service_data"], {"brightness": 3})
check("last_triggered lands on the step",
      legacy["steps"][0]["last_triggered"], "2026-09-19T22:10:38-05:00")
check("and is kept as the schedule rollup",
      legacy["last_triggered"], "2026-09-19T22:10:38-05:00")

print("\nnormalise_schedule")
s = store.normalise_schedule({
    "name": "Garden Irrigation", "day_set": "daily",
    "pattern": {"type": "occurrences", "occurrences": ["06:00"]},
    "actions": [{"service": "switch.turn_on", "entity_id": "switch.b_hyve_node_garden"}],
})
check("actions are repaired on load, not just on create",
      s["steps"][0]["actions"][0].get("service_data"), {})
check("an id is generated", bool(s["schedule_id"]), True)
check("last_triggered starts empty", s["last_triggered"], None)

kept = store.normalise_schedule({**s, "last_triggered": "2026-09-19T18:50:33-05:00",
                                 "name": "renamed"})
check("an edit preserves last_triggered",
      kept["last_triggered"], "2026-09-19T18:50:33-05:00")
check("an interval without every_minutes still normalises",
      store.normalise_schedule({"pattern": {"type": "interval"}})
           ["steps"][0]["pattern"]["every_minutes"], 60)

print("\nmerge_steps — an edit must not erase what it did not mention")
stored = store.normalise_schedule({
    "name": "Garden Lights", "day_set": "daily",
    "steps": [
        {"step_id": "s1", "name": "On",
         "pattern": {"type": "occurrences", "occurrences": ["sunset+00:15:00"]},
         "actions": [{"service": "light.turn_on", "entity_id": "light.garden_lights"}],
         "last_triggered": "2026-09-19T19:12:00-05:00"},
        {"step_id": "s2", "name": "Off",
         "pattern": {"type": "occurrences", "occurrences": ["sunrise+00:15:00"]},
         "actions": [{"service": "light.turn_off", "entity_id": "light.garden_lights"}]},
    ],
})["steps"]

# What the card sends: name + pattern + enabled, and nothing else.
edited = store.merge_steps(stored, [
    {"step_id": "s1", "name": "On", "enabled": True,
     "pattern": {"type": "occurrences", "occurrences": ["sunset+00:30:00"]}},
    {"step_id": "s2", "name": "Off", "enabled": False,
     "pattern": {"type": "occurrences", "occurrences": ["sunrise+00:15:00"]}},
])
check("actions survive an edit that never mentions them",
      edited[0]["actions"][0]["service"], "light.turn_on")
check("so does the step's own run history",
      edited[0]["last_triggered"], "2026-09-19T19:12:00-05:00")
check("the edited field actually changes",
      edited[0]["pattern"]["occurrences"], ["sunset+00:30:00"])
check("a step can still be disabled", edited[1]["enabled"], False)
check("omitting a step still deletes it",
      [s["step_id"] for s in store.merge_steps(stored, [{"step_id": "s1"}])], ["s1"])
check("an unknown id is still an insert",
      store.merge_steps(stored, [{"step_id": "s9", "name": "New"}])[0]["name"], "New")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S)")
    sys.exit(1)
print("all schedule-store tests passed")
