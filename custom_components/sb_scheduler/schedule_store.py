"""Schedule storage.

The schema is two XOR choices: which days (a day-set) and what times within a
day (discrete occurrences, or an interval). That symmetry is deliberate -- it is
what makes the eventual card two radio groups instead of a form of exceptions.
"""

from __future__ import annotations

import logging
import secrets

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import (
    ATTR_LAST_TRIGGERED,
    CONF_ACTIONS,
    CONF_DAY_SET,
    CONF_ENABLED,
    CONF_EVERY_MINUTES,
    CONF_OCCURRENCES,
    CONF_PATTERN,
    CONF_SCHEDULE_ID,
    CONF_START,
    CONF_STEP_ID,
    CONF_STEPS,
    CONF_STOP,
    DOMAIN,
    PATTERN_INTERVAL,
    PATTERN_OCCURRENCES,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.schedules"
STORAGE_VERSION = 1
SAVE_DELAY = 5


def normalise_action(action: dict) -> dict:
    """Give an action the shape the inherited action engine assumes.

    `parse_service_call` reads `data[ATTR_SERVICE_DATA]` with an unconditional
    subscript, so an action without `service_data` raises KeyError the moment
    the schedule fires -- not when it is created, which makes it a landmine.
    `data` is accepted as an alias because that is the key
    scheduler-component's own storage uses.
    """
    out = dict(action or {})
    if "service_data" not in out:
        out["service_data"] = out.pop("data", None) or {}
    else:
        out.pop("data", None)
    return out


def normalise_pattern(pattern: dict | None) -> dict:
    """One canonical shape for a time pattern."""
    pattern = dict(pattern or {})
    if (pattern.get("type") or PATTERN_OCCURRENCES) == PATTERN_INTERVAL:
        return {
            "type": PATTERN_INTERVAL,
            CONF_START: pattern.get(CONF_START, "00:00"),
            CONF_STOP: pattern.get(CONF_STOP, "23:59"),
            CONF_EVERY_MINUTES: int(pattern.get(CONF_EVERY_MINUTES, 60)),
        }
    return {
        "type": PATTERN_OCCURRENCES,
        CONF_OCCURRENCES: [str(t) for t in (pattern.get(CONF_OCCURRENCES) or [])],
    }


def allocate_step_ids(steps: list[dict]) -> list[str]:
    """One id per step: keep the ids already assigned, never reuse one.

    Positional ids collide. Delete the first of two steps and add a new one and
    both land on `s2` -- after which `merge_steps` overlays two steps onto one
    stored step, and `_handlers` / `_step_next`, which are keyed by step id,
    collapse the pair so one of them silently never fires.

    An id that an earlier step in the same list already claimed is reallocated
    for the same reason.
    """
    taken = {s.get(CONF_STEP_ID) for s in steps if isinstance(s, dict)} - {None, ""}
    used: set[str] = set()
    out: list[str] = []
    counter = 0
    for step in steps:
        sid = step.get(CONF_STEP_ID) if isinstance(step, dict) else None
        if not sid or sid in used:
            counter += 1
            while f"s{counter}" in taken or f"s{counter}" in used:
                counter += 1
            sid = f"s{counter}"
        used.add(sid)
        out.append(sid)
    return out


def normalise_step(data: dict, index: int, step_id: str | None = None) -> dict:
    """A step = a time pattern + the actions to run at it."""
    data = dict(data or {})
    return {
        CONF_STEP_ID: step_id or data.get(CONF_STEP_ID) or f"s{index + 1}",
        "name": data.get("name") or f"Step {index + 1}",
        CONF_ENABLED: bool(data.get(CONF_ENABLED, True)),
        CONF_PATTERN: normalise_pattern(data.get(CONF_PATTERN)),
        CONF_ACTIONS: [normalise_action(a) for a in (data.get(CONF_ACTIONS) or [])],
        "conditions": list(data.get("conditions") or []),
        "condition_type": data.get("condition_type") or "and",
        "track_conditions": bool(data.get("track_conditions", False)),
        ATTR_LAST_TRIGGERED: data.get(ATTR_LAST_TRIGGERED),
    }


def merge_steps(current: list[dict], incoming: list[dict]) -> list[dict]:
    """Overlay each incoming step onto the stored step with the same id.

    An edit that sends only what it changed must not erase what it did not
    mention. The card edits a step's name and pattern and knows nothing about
    `actions`, so a plain list replacement would silently empty them -- and,
    like the `service_data` landmine, the damage would only show at FIRE time.

    The resulting list is exactly `incoming`, so omitting a step still deletes
    it and an unrecognised id still adds one. Only the CONTENT is merged.
    """
    by_id = {s[CONF_STEP_ID]: s for s in current if s.get(CONF_STEP_ID)}
    return [{**by_id.get(step.get(CONF_STEP_ID), {}), **step} for step in incoming]


def normalise_schedule(data: dict) -> dict:
    """Fill in defaults so the rest of the code can stop checking."""
    steps = data.get(CONF_STEPS)
    if not steps:
        # MIGRATION: a pre-steps schedule is one implicit step built from its
        # top-level pattern/actions. Nothing on disk needs rewriting.
        steps = [{
            "name": data.get("name") or "Run",
            CONF_PATTERN: data.get(CONF_PATTERN),
            CONF_ACTIONS: data.get(CONF_ACTIONS),
            "conditions": data.get("conditions"),
            "condition_type": data.get("condition_type"),
            "track_conditions": data.get("track_conditions"),
            ATTR_LAST_TRIGGERED: data.get(ATTR_LAST_TRIGGERED),
        }]

    return {
        CONF_SCHEDULE_ID: data.get(CONF_SCHEDULE_ID) or secrets.token_hex(3),
        "name": data.get("name") or "Schedule",
        CONF_ENABLED: bool(data.get(CONF_ENABLED, True)),
        CONF_DAY_SET: data.get(CONF_DAY_SET) or "daily",
        CONF_STEPS: [
            normalise_step(s, i, sid)
            for i, (s, sid) in enumerate(zip(steps, allocate_step_ids(steps)))
        ],
        # Rollup across steps. Carried through every edit: an edit must not
        # erase the run history.
        ATTR_LAST_TRIGGERED: data.get(ATTR_LAST_TRIGGERED),
    }


class ScheduleStore:
    """Persisted schedules for one config entry."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.schedules: dict[str, dict] = {}
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data:
            for item in data.get("schedules", []):
                schedule = normalise_schedule(item)
                self.schedules[schedule[CONF_SCHEDULE_ID]] = schedule
        _LOGGER.debug("Loaded %d schedule(s)", len(self.schedules))

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {"schedules": list(self.schedules.values())}, SAVE_DELAY
        )

    @callback
    def async_create(self, data: dict) -> dict:
        schedule = normalise_schedule(data)
        self.schedules[schedule[CONF_SCHEDULE_ID]] = schedule
        self._save()
        return schedule

    @callback
    def async_update(self, schedule_id: str, changes: dict) -> dict | None:
        current = self.schedules.get(schedule_id)
        if current is None:
            return None
        changes = dict(changes)
        if CONF_STEPS in changes:
            changes[CONF_STEPS] = merge_steps(
                current.get(CONF_STEPS, []), changes[CONF_STEPS]
            )
        merged = normalise_schedule({**current, **changes, CONF_SCHEDULE_ID: schedule_id})
        self.schedules[schedule_id] = merged
        self._save()
        return merged

    @callback
    def async_record_step_trigger(self, schedule_id: str, step_id: str, when: str) -> None:
        """Stamp one step, and the schedule rollup, as having just fired."""
        schedule = self.schedules.get(schedule_id)
        if schedule is None:
            return
        for step in schedule.get(CONF_STEPS, []):
            if step[CONF_STEP_ID] == step_id:
                step[ATTR_LAST_TRIGGERED] = when
        schedule[ATTR_LAST_TRIGGERED] = when
        self._save()

    @callback
    def async_record_trigger(self, schedule_id: str, when: str) -> None:
        """Remember when a schedule last fired.

        Persisted rather than held in memory so a restart does not erase it --
        "did last night's run happen?" is exactly the question you ask after a
        restart.
        """
        schedule = self.schedules.get(schedule_id)
        if schedule is None:
            return
        schedule[ATTR_LAST_TRIGGERED] = when
        self._save()

    @callback
    def async_delete(self, schedule_id: str) -> bool:
        if schedule_id not in self.schedules:
            return False
        self.schedules.pop(schedule_id)
        self._save()
        return True
