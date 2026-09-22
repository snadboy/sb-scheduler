"""A switch entity per schedule: on = enabled, off = disabled."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from .actions import ActionHandler
from .const import (
    ATTR_LAST_TRIGGERED,
    ATTR_NEXT_TRIGGER,
    CONF_ACTIONS,
    CONF_DAY_SET,
    CONF_NEGATE,
    CONF_ENABLED,
    CONF_PATTERN,
    CONF_SCHEDULE_ID,
    CONF_STEP_ID,
    CONF_STEPS,
    DOMAIN,
    SIGNAL_DAY_SETS_UPDATED,
    SIGNAL_SCHEDULES_UPDATED,
)
from .day_set import NegatedDaySet
from .timer import describe_times, next_trigger, times_on

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
        "run_now", {vol.Optional("step_id"): cv.string}, "async_run_now"
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
        self._attr_device_info = data.device
        self._timer_unsub = None
        self._next: object | None = None
        self._next_steps: list[str] = []
        self._step_next: dict[str, str] = {}
        # One handler PER STEP: an unavailable target for "off" must not cancel
        # the queue that "on" is waiting in.
        self._handlers: dict[str, ActionHandler] = {}

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
        today = dt_util.now().date()
        steps = []
        for step in schedule.get(CONF_STEPS, []):
            pattern = step.get(CONF_PATTERN) or {}
            steps.append({
                CONF_STEP_ID: step[CONF_STEP_ID],
                "name": step.get("name"),
                CONF_ENABLED: step.get(CONF_ENABLED, True),
                # The RAW pattern is what an editor must round-trip; `times`
                # is the resolution for today, and editing that would lose an
                # interval or a sun-relative occurrence.
                CONF_PATTERN: pattern,
                "times": [
                    t.strftime("%H:%M")
                    for t in times_on(pattern, today, self._sun)
                ],
                # Which sun event (if any) each time tracks. The card cannot
                # zip `times` against the stored pattern — resolution sorts by
                # clock time and reorders them.
                "times_detail": describe_times(pattern, today, self._sun),
                CONF_ACTIONS: step.get(CONF_ACTIONS, []),
                ATTR_NEXT_TRIGGER: self._step_next.get(step[CONF_STEP_ID]),
                ATTR_LAST_TRIGGERED: step.get(ATTR_LAST_TRIGGERED),
            })
        return {
            CONF_SCHEDULE_ID: self.schedule_id,
            CONF_DAY_SET: schedule.get(CONF_DAY_SET),
            CONF_NEGATE: bool(schedule.get(CONF_NEGATE)),
            CONF_STEPS: steps,
            # Rollup across steps, so the entity still reads at a glance.
            ATTR_NEXT_TRIGGER: self._next.isoformat() if self._next else None,
            ATTR_LAST_TRIGGERED: schedule.get(ATTR_LAST_TRIGGERED),
        }

    async def async_added_to_hass(self) -> None:
        self._sync_handlers()
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
        for handler in self._handlers.values():
            await handler.async_empty_queue()
        self._handlers.clear()

    def _sync_handlers(self) -> None:
        """One ActionHandler per step, created lazily and keyed by step id."""
        for step in self.schedule.get(CONF_STEPS, []):
            sid = step[CONF_STEP_ID]
            if sid not in self._handlers:
                self._handlers[sid] = ActionHandler(
                    self.hass, f"{self.schedule_id}:{sid}"
                )

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
    def _sun(self, event: str, day):
        """Sunrise/sunset for a SPECIFIC date.

        `sun.next_rising` (what upstream reads) only knows the next one, so it
        cannot answer "when does the sun set three weeks from Tuesday" --
        the same now-only limitation as the workday sensor.
        """
        try:
            moment = get_astral_event_date(self.hass, event, day)
        except Exception:  # noqa: BLE001 - never let astral break arming
            _LOGGER.exception("Could not resolve %s for %s", event, day)
            return None
        return dt_util.as_local(moment) if moment else None

    @callback
    def _cancel(self) -> None:
        if self._timer_unsub:
            self._timer_unsub()
            self._timer_unsub = None

    @callback
    def _rearm(self) -> None:
        """Recompute every step's next trigger and arm for the earliest."""
        self._cancel()
        self._next = None
        self._next_steps = []
        self._step_next = {}

        schedule = self.schedule
        if schedule and self.is_on:
            self._sync_handlers()
            day_set = self._data.day_sets.get(schedule.get(CONF_DAY_SET))
            if day_set is not None and schedule.get(CONF_NEGATE):
                day_set = NegatedDaySet(day_set)   # "every day NOT in it"
            if day_set is None:
                # Loud, because the alternative is a schedule that looks armed
                # and never fires -- the exact upstream failure this replaces.
                _LOGGER.error(
                    "Schedule '%s' refers to unknown day-set '%s'; it will not run",
                    schedule.get("name"),
                    schedule.get(CONF_DAY_SET),
                )
            else:
                now = dt_util.now()
                soonest = None
                for step in schedule.get(CONF_STEPS, []):
                    if not step.get(CONF_ENABLED, True):
                        continue
                    when = next_trigger(
                        step.get(CONF_PATTERN), day_set, now, self._sun,
                        label=f"{schedule.get('name')} / {step.get('name')}",
                    )
                    if when is None:
                        continue
                    self._step_next[step[CONF_STEP_ID]] = when.isoformat()
                    if soonest is None or when < soonest:
                        soonest = when
                # Several steps can share a moment; fire all of them.
                if soonest is not None:
                    self._next = soonest
                    self._next_steps = [
                        s[CONF_STEP_ID] for s in schedule.get(CONF_STEPS, [])
                        if self._step_next.get(s[CONF_STEP_ID]) == soonest.isoformat()
                    ]
                    self._timer_unsub = async_track_point_in_time(
                        self.hass, self._handle_trigger, soonest
                    )
                    _LOGGER.debug(
                        "Schedule '%s' armed for %s (steps: %s)",
                        schedule.get("name"), soonest, self._next_steps,
                    )

        if self.hass is not None:
            self.async_write_ha_state()

    async def _handle_trigger(self, _now) -> None:
        """Fire whichever steps are due, then arm the next."""
        self._timer_unsub = None
        due = list(self._next_steps)
        await self._run_steps(due)
        self._rearm()

    async def _run_steps(self, step_ids: list[str]) -> None:
        schedule = self.schedule
        now = dt_util.now().isoformat()
        for step in schedule.get(CONF_STEPS, []):
            if step[CONF_STEP_ID] not in step_ids:
                continue
            _LOGGER.debug(
                "Schedule '%s' step '%s' triggered",
                schedule.get("name"), step.get("name"),
            )
            # Record the firing, not the outcome: actions retry asynchronously
            # when a target is unavailable, so "it ran" and "it succeeded" are
            # different questions. Failures are reported by the action queue.
            self._data.schedules.async_record_step_trigger(
                self.schedule_id, step[CONF_STEP_ID], now
            )
            handler = self._handlers.get(step[CONF_STEP_ID])
            if handler is not None:
                await handler.async_queue_actions({
                    "conditions": step.get("conditions", []),
                    "actions": step.get(CONF_ACTIONS, []),
                    "condition_type": step.get("condition_type", "and"),
                    "track_conditions": step.get("track_conditions", False),
                })

    async def async_run_now(self, step_id: str | None = None) -> None:
        """Run a step now, ignoring the day-set. Default: every enabled step."""
        self._sync_handlers()
        ids = [
            s[CONF_STEP_ID] for s in self.schedule.get(CONF_STEPS, [])
            if (step_id is None and s.get(CONF_ENABLED, True)) or s[CONF_STEP_ID] == step_id
        ]
        await self._run_steps(ids)
        self._rearm()
