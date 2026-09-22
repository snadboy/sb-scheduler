"""A refresh button, so "recompute the day types now" can sit on a dashboard.

An event added to a source calendar is otherwise picked up within that
calendar's own poll plus the periodic refresh (REFRESH_INTERVAL_MINUTES).
This is for the impatient case, and it is a button rather than a service
call because a button is what a dashboard can hold.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([RefreshButton(entry, hass.data[DOMAIN][entry.entry_id])])


class RefreshButton(ButtonEntity):
    _attr_should_poll = False
    _attr_has_entity_name = False
    _attr_name = "SB Scheduler Refresh"
    _attr_icon = "mdi:calendar-refresh"

    def __init__(self, entry: ConfigEntry, data) -> None:
        self._data = data
        self._attr_unique_id = f"{entry.entry_id}_refresh"

    async def async_press(self) -> None:
        await self._data.day_sets.async_refresh("button")
