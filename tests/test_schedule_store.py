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

print("\nnormalise_schedule")
s = store.normalise_schedule({
    "name": "Garden Irrigation", "day_set": "daily",
    "pattern": {"type": "occurrences", "occurrences": ["06:00"]},
    "actions": [{"service": "switch.turn_on", "entity_id": "switch.b_hyve_node_garden"}],
})
check("actions are repaired on load, not just on create",
      s["actions"][0].get("service_data"), {})
check("an id is generated", bool(s["schedule_id"]), True)
check("last_triggered starts empty", s["last_triggered"], None)

kept = store.normalise_schedule({**s, "last_triggered": "2026-09-19T18:50:33-05:00",
                                 "name": "renamed"})
check("an edit preserves last_triggered",
      kept["last_triggered"], "2026-09-19T18:50:33-05:00")
check("an interval without every_minutes still normalises",
      store.normalise_schedule({"pattern": {"type": "interval"}})["pattern"]["every_minutes"], 60)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S)")
    sys.exit(1)
print("all schedule-store tests passed")
