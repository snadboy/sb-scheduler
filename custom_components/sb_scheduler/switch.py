"""A switch entity per schedule: on = enabled, off = disabled."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .actions import ActionHandler
from .const import (
    ATTR_NEXT_TRIGGER,
    CONF_ACTIONS,
    CONF_DAY_SET,
    CONF_ENABLED,
    CONF_PATTERN,
    CONF_SCHEDULE_ID,
    DOMAIN,
    SIGNAL_DAY_SETS_UPDATED,
    SIGNAL_SCHEDULES_UPDATED,
)
from .timer import next_trigger, occurrence_times

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create an entity per stored schedule, and for any added later."""
    data = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _sync() -> None:
        new = [
            ScheduleEntity(entry, data, schedule_id)
            for schedule_id in data.schedules.schedules
            if schedule_id not in known
        ]
        known.update(e.schedule_id for e in new)
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_SCHEDULES_UPDATED, _sync)
    )

    # Fire a schedule's actions now, ignoring its day-set. The obvious way to
    # test a schedule without waiting for its next eligible date.
    entity_platform.async_get_current_platform().async_register_entity_service(
        "run_now", {}, "async_run_now"
    )


class ScheduleEntity(SwitchEntity):
    """One schedule."""

    _attr_should_poll = False
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, entry: ConfigEntry, data, schedule_id: str) -> None:
        self._entry = entry
        self._data = data
        self.schedule_id = schedule_id
        self._attr_unique_id = f"{entry.entry_id}_schedule_{schedule_id}"
        self._timer_unsub = None
        self._next: object | None = None
        self._handler: ActionHandler | None = None

    # --- plumbing ----------------------------------------------------------
    @property
    def schedule(self) -> dict:
        return self._data.schedules.schedules.get(self.schedule_id, {})

    @property
    def name(self) -> str:
        return self.schedule.get("name", "Schedule")

    @property
    def is_on(self) -> bool:
        return bool(self.schedule.get(CONF_ENABLED, True))

    @property
    def available(self) -> bool:
        return self.schedule_id in self._data.schedules.schedules

    @property
    def extra_state_attributes(self) -> dict:
        schedule = self.schedule
        pattern = schedule.get(CONF_PATTERN) or {}
        return {
            CONF_SCHEDULE_ID: self.schedule_id,
            CONF_DAY_SET: schedule.get(CONF_DAY_SET),
            "pattern_type": pattern.get("type"),
            # The RAW pattern, so an editor can round-trip an interval's
            # start/stop/every_minutes. `times` below is the expansion, which
            # is display-only — editing that would lose the interval.
            CONF_PATTERN: pattern,
            "times": [t.isoformat() for t in occurrence_times(pattern)],
            ATTR_NEXT_TRIGGER: self._next.isoformat() if self._next else None,
            CONF_ACTIONS: schedule.get(CONF_ACTIONS, []),
        }

    async def async_added_to_hass(self) -> None:
        self._handler = ActionHandler(self.hass, self.schedule_id)
        # A day-set change can move every trigger that depends on it.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_DAY_SETS_UPDATED, self._handle_day_sets_updated
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_SCHEDULES_UPDATED, self._handle_schedule_updated
            )
        )
        self._rearm()

    async def async_will_remove_from_hass(self) -> None:
        self._cancel()
        if self._handler:
            await self._handler.async_empty_queue()

    @callback
    def _handle_day_sets_updated(self) -> None:
        self._rearm()

    @callback
    def _handle_schedule_updated(self) -> None:
        if self.available:
            self._rearm()

    # --- switching ---------------------------------------------------------
    async def async_turn_on(self, **kwargs) -> None:
        self._data.schedules.async_update(self.schedule_id, {CONF_ENABLED: True})
        self._rearm()

    async def async_turn_off(self, **kwargs) -> None:
        self._data.schedules.async_update(self.schedule_id, {CONF_ENABLED: False})
        self._rearm()

    # --- timing ------------------------------------------------------------
    @callback
    def _cancel(self) -> None:
        if self._timer_unsub:
            self._timer_unsub()
            self._timer_unsub = None

    @callback
    def _rearm(self) -> None:
        """Recompute the next trigger and arm a timer for it."""
        self._cancel()
        self._next = None

        schedule = self.schedule
        if schedule and self.is_on:
            day_set = self._data.day_sets.get(schedule.get(CONF_DAY_SET))
            if day_set is None:
                # Loud, because the alternative is a schedule that looks armed
                # and never fires -- the exact upstream failure this replaces.
                _LOGGER.error(
                    "Schedule '%s' refers to unknown day-set '%s'; it will not run",
                    schedule.get("name"),
                    schedule.get(CONF_DAY_SET),
                )
            else:
                self._next = next_trigger(schedule, day_set, dt_util.now())
                if self._next:
                    self._timer_unsub = async_track_point_in_time(
                        self.hass, self._handle_trigger, self._next
                    )
                    _LOGGER.debug(
                        "Schedule '%s' armed for %s", schedule.get("name"), self._next
                    )

        if self.hass is not None:
            self.async_write_ha_state()

    async def _handle_trigger(self, _now) -> None:
        """Fire, then arm the following occurrence."""
        self._timer_unsub = None
        schedule = self.schedule
        _LOGGER.debug("Schedule '%s' triggered", schedule.get("name"))

        if self._handler is not None:
            await self._handler.async_queue_actions(
                {
                    "conditions": schedule.get("conditions", []),
                    "actions": schedule.get(CONF_ACTIONS, []),
                    "condition_type": schedule.get("condition_type", "and"),
                    "track_conditions": schedule.get("track_conditions", False),
                }
            )
        self._rearm()

    async def async_run_now(self) -> None:
        """Execute the actions immediately, ignoring the day-set."""
        await self._handle_trigger(None)
