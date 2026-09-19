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
    CONF_ACTIONS,
    CONF_DAY_SET,
    CONF_ENABLED,
    CONF_EVERY_MINUTES,
    CONF_OCCURRENCES,
    CONF_PATTERN,
    CONF_SCHEDULE_ID,
    CONF_START,
    CONF_STOP,
    DOMAIN,
    PATTERN_INTERVAL,
    PATTERN_OCCURRENCES,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.schedules"
STORAGE_VERSION = 1
SAVE_DELAY = 5


def normalise_schedule(data: dict) -> dict:
    """Fill in defaults so the rest of the code can stop checking."""
    pattern = dict(data.get(CONF_PATTERN) or {})
    kind = pattern.get("type") or PATTERN_OCCURRENCES

    if kind == PATTERN_INTERVAL:
        pattern = {
            "type": PATTERN_INTERVAL,
            CONF_START: pattern.get(CONF_START, "00:00"),
            CONF_STOP: pattern.get(CONF_STOP, "23:59"),
            CONF_EVERY_MINUTES: int(pattern.get(CONF_EVERY_MINUTES, 60)),
        }
    else:
        occurrences = pattern.get(CONF_OCCURRENCES) or []
        pattern = {
            "type": PATTERN_OCCURRENCES,
            CONF_OCCURRENCES: [str(t) for t in occurrences],
        }

    return {
        CONF_SCHEDULE_ID: data.get(CONF_SCHEDULE_ID) or secrets.token_hex(3),
        "name": data.get("name") or "Schedule",
        CONF_ENABLED: bool(data.get(CONF_ENABLED, True)),
        CONF_DAY_SET: data.get(CONF_DAY_SET) or "daily",
        CONF_PATTERN: pattern,
        CONF_ACTIONS: list(data.get(CONF_ACTIONS) or []),
        "conditions": list(data.get("conditions") or []),
        "condition_type": data.get("condition_type") or "and",
        "track_conditions": bool(data.get("track_conditions", False)),
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
        merged = normalise_schedule({**current, **changes, CONF_SCHEDULE_ID: schedule_id})
        self.schedules[schedule_id] = merged
        self._save()
        return merged

    @callback
    def async_delete(self, schedule_id: str) -> bool:
        if schedule_id not in self.schedules:
            return False
        self.schedules.pop(schedule_id)
        self._save()
        return True
