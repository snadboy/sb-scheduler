"""Publish each day-set as a calendar entity.

Costs little and makes a day-set debuggable by looking at it in Home Assistant,
which matters the first time School Day disagrees with you.
"""

from __future__ import annotations

import datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_DAY_SETS_UPDATED
from .day_set import DaySet
from .registry import DaySetRegistry


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up one calendar per day-set."""
    registry: DaySetRegistry = hass.data[DOMAIN][entry.entry_id].day_sets
    async_add_entities(
        DaySetCalendar(entry, registry, day_set)
        for day_set in registry.day_sets.values()
    )


class DaySetCalendar(CalendarEntity):
    """A read-only calendar showing which dates are in a day-set."""

    _attr_should_poll = False
    _attr_has_entity_name = False

    def __init__(
        self, entry: ConfigEntry, registry: DaySetRegistry, day_set: DaySet
    ) -> None:
        self._registry = registry
        self._day_set = day_set
        self._attr_name = day_set.name
        self._attr_unique_id = f"{entry.entry_id}_{day_set.id}"
        self._attr_icon = "mdi:calendar-check"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_DAY_SETS_UPDATED, self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        """Enough to answer "why is today not a school day?" at a glance."""
        today = dt_util.now().date()
        next_date = self._day_set.next_date_on_or_after(today)
        return {
            "day_set_id": self._day_set.id,
            "eligible_today": self._day_set.is_eligible(today),
            "next_date": next_date.isoformat() if next_date else None,
            "weekdays": self._day_set.weekdays,
            "base_calendars": self._day_set.base_calendars,
            "exclude_calendars": self._day_set.exclude_calendars,
            "force_calendars": self._day_set.force_calendars,
            "inverted": self._day_set.invert,
            "offset_days": self._day_set.offset_days,
        }

    @property
    def event(self) -> CalendarEvent | None:
        """The current run if today is eligible, else the next one."""
        today = dt_util.now().date()
        start = self._day_set.next_date_on_or_after(today)
        if start is None:
            return None
        runs = self._day_set.runs(start, start + datetime.timedelta(days=60))
        if not runs:
            return None
        run_start, run_end = runs[0]
        return self._as_event(run_start, run_end)

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[CalendarEvent]:
        """Consecutive eligible dates, merged into one event each."""
        lo = dt_util.as_local(start_date).date()
        hi = dt_util.as_local(end_date).date()
        return [
            self._as_event(run_start, run_end)
            for run_start, run_end in self._day_set.runs(lo, hi)
        ]

    def _as_event(
        self, run_start: datetime.date, run_end: datetime.date
    ) -> CalendarEvent:
        # All-day events are half-open, so end is the morning after the run.
        return CalendarEvent(
            summary=self._day_set.name,
            start=run_start,
            end=run_end + datetime.timedelta(days=1),
            uid=f"{self._day_set.id}-{run_start.isoformat()}",
        )
