"""One sensor listing every day-set.

The card used to discover day-sets by scanning for calendar entities carrying
a `day_set_id`. Once a day-set can opt out of having a calendar, that stops
being a complete list — so this single entity is the roster instead.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_DAY_SETS_UPDATED
from .registry import DaySetRegistry

ROSTER_UNIQUE_SUFFIX = "day_sets"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    registry: DaySetRegistry = hass.data[DOMAIN][entry.entry_id].day_sets
    async_add_entities([DaySetRoster(entry, registry)])


class DaySetRoster(SensorEntity):
    """State = how many day-sets; attributes = what they are."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_name = "SB Scheduler Day-sets"
    _attr_icon = "mdi:calendar-multiple"

    def __init__(self, entry: ConfigEntry, registry: DaySetRegistry) -> None:
        self._registry = registry
        self._attr_unique_id = f"{entry.entry_id}_{ROSTER_UNIQUE_SUFFIX}"

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
    def native_value(self) -> int:
        return len(self._registry.day_sets)

    @property
    def extra_state_attributes(self) -> dict:
        today = dt_util.now().date()
        roster = []
        for ds in self._registry.day_sets.values():
            nxt = ds.next_date_on_or_after(today)
            roster.append({
                "id": ds.id,
                "name": ds.name,
                "calendar": ds.expose_calendar,
                "base_day_set": ds.base_day_set or None,
                "pick": ds.describe_pick() or None,
                "months": ds.months or None,
                "next_date": nxt.isoformat() if nxt else None,
            })
        # The marker the card looks for, so it never has to guess by name.
        return {"roster": "sb_scheduler", "day_sets": roster}
