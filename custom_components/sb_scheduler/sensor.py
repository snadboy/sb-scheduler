"""One sensor listing every day-set — the card's read path for editing them.

The card used to discover day-sets by scanning for calendar entities carrying
a `day_set_id`. Once a day-set can opt out of having a calendar, that stops
being a complete list — so this single entity is the roster instead, and it
carries each day-set's full stored config so the card can edit it without a
websocket API.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DAY_SET,
    CONF_DAY_SETS,
    CONF_ID,
    DOMAIN,
    SIGNAL_DAY_SETS_UPDATED,
    SIGNAL_SCHEDULES_UPDATED,
)

ROSTER_UNIQUE_SUFFIX = "day_sets"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([DaySetRoster(entry, hass.data[DOMAIN][entry.entry_id])])


class DaySetRoster(SensorEntity):
    """State = how many day-sets; attributes = what they are, in full."""

    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_name = "SB Scheduler Day types"
    _attr_icon = "mdi:calendar-multiple"

    def __init__(self, entry: ConfigEntry, data) -> None:
        self._entry = entry
        self._data = data
        self._attr_unique_id = f"{entry.entry_id}_{ROSTER_UNIQUE_SUFFIX}"

    async def async_added_to_hass(self) -> None:
        for signal in (SIGNAL_DAY_SETS_UPDATED, SIGNAL_SCHEDULES_UPDATED):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, signal, self._handle_update)
            )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return len(self._data.day_sets.day_sets)

    @property
    def extra_state_attributes(self) -> dict:
        today = dt_util.now().date()
        configs = {c[CONF_ID]: c for c in self._entry.options.get(CONF_DAY_SETS, [])}
        schedules = list(self._data.schedules.schedules.values())
        roster = []
        for ds in self._data.day_sets.day_sets.values():
            nxt = ds.next_date_on_or_after(today)
            roster.append({
                "id": ds.id,
                "name": ds.name,
                "calendar": ds.expose_calendar,
                "base_day_set": ds.base_day_set or None,
                "pick": ds.describe_pick() or None,
                "months": ds.months or None,
                "next_date": nxt.isoformat() if nxt else None,
                # What the editor round-trips: the stored config, verbatim.
                "config": configs.get(ds.id, {}),
                # What stops it being deleted, so the card can say so up front.
                "used_by": {
                    "schedules": [s.get("name") for s in schedules if s.get(CONF_DAY_SET) == ds.id],
                    "day_sets": [o.id for o in self._data.day_sets.day_sets.values()
                                 if ds.id in o.depends_on],
                },
            })
        # The marker the card looks for, so it never has to guess by name.
        return {"roster": "sb_scheduler", "day_sets": roster}
