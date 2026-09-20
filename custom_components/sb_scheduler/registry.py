"""Holds the configured day-sets and keeps their windows fresh."""

from __future__ import annotations

import datetime
import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DAY_SETS,
    HORIZON_DAYS,
    PAST_DAYS,
    REFRESH_HOUR,
    REFRESH_MINUTE,
    SIGNAL_DAY_SETS_UPDATED,
)
from .day_set import DaySet

_LOGGER = logging.getLogger(__name__)


class DaySetRegistry:
    """Owns every day-set for one config entry."""

    def __init__(self, hass: HomeAssistant, options: dict) -> None:
        self.hass = hass
        self.day_sets: dict[str, DaySet] = {
            cfg["id"]: DaySet.from_config(cfg)
            for cfg in options.get(CONF_DAY_SETS, [])
        }
        self._unsubs: list = []

    def get(self, day_set_id: str) -> DaySet | None:
        return self.day_sets.get(day_set_id)

    async def async_setup(self) -> None:
        """Compute the first window and arm the refresh triggers."""
        await self.async_refresh()

        # Just after midnight, so "today" never falls off the front of the window.
        self._unsubs.append(
            async_track_time_change(
                self.hass,
                self._handle_midnight,
                hour=REFRESH_HOUR,
                minute=REFRESH_MINUTE,
                second=0,
            )
        )

        # A source calendar changing is the other thing that can invalidate us.
        sources = sorted(
            {e for ds in self.day_sets.values() for e in ds.source_entities}
        )
        if sources:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, sources, self._handle_source_change
                )
            )
            _LOGGER.debug("Watching %d source calendar(s): %s", len(sources), sources)

    async def async_refresh(self) -> None:
        """Recompute every day-set from today forward."""
        today = dt_util.now().date()
        # Start before today so the published calendars can render the current
        # month, while keeping the full forward horizon.
        start = today - datetime.timedelta(days=PAST_DAYS)
        for day_set in self.day_sets.values():
            await day_set.async_refresh(
                self.hass, start, days=HORIZON_DAYS + PAST_DAYS
            )
        async_dispatcher_send(self.hass, SIGNAL_DAY_SETS_UPDATED)

    async def _handle_midnight(self, _now) -> None:
        await self.async_refresh()

    @callback
    def _handle_source_change(self, event) -> None:
        self.hass.async_create_task(self.async_refresh())

    async def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
