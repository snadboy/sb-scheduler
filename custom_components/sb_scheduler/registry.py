"""Holds the configured day-sets and keeps their windows fresh."""

from __future__ import annotations

import datetime
import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DAY_SETS,
    HORIZON_DAYS,
    MAX_ANCHOR_AGE_DAYS,
    MISSING_SOURCE_RETRY_SECONDS,
    PAST_DAYS,
    REFRESH_HOUR,
    REFRESH_MINUTE,
    SIGNAL_DAY_SETS_UPDATED,
)
from .day_set import DaySet, order_by_dependency

_LOGGER = logging.getLogger(__name__)


class DaySetRegistry:
    """Owns every day-set for one config entry."""

    def __init__(self, hass: HomeAssistant, options: dict) -> None:
        self.hass = hass
        configs = list(options.get(CONF_DAY_SETS, []))
        # Dependency order is fixed at construction: a derived day-set must be
        # evaluated after the one it builds on, every refresh.
        self._ordered, unresolved = order_by_dependency(configs)
        for cfg in unresolved:
            _LOGGER.error(
                "Day-set '%s' builds on '%s', which is missing or part of a "
                "cycle; it will be evaluated with an empty base",
                cfg.get("id"), cfg.get("base_day_set"),
            )
        self._unresolved = {c["id"] for c in unresolved}
        self.day_sets: dict[str, DaySet] = {
            cfg["id"]: DaySet.from_config(cfg) for cfg in self._ordered
        }
        self._unsubs: list = []
        self._retry_unsub = None

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

    def _window_start(self, today: datetime.date) -> datetime.date:
        """Where the computed window begins.

        Normally PAST_DAYS back, so the published calendars can render the
        current month. An "every N from <anchor>" day-set needs the window to
        reach its anchor, or the stride cannot be counted — so the start is
        pulled back to the oldest anchor, within a hard limit.
        """
        start = today - datetime.timedelta(days=PAST_DAYS)
        floor = today - datetime.timedelta(days=MAX_ANCHOR_AGE_DAYS)
        for ds in self.day_sets.values():
            anchor = ds.anchor_date
            if anchor is None or anchor >= start:
                continue
            if anchor < floor:
                _LOGGER.warning(
                    "Day-set '%s' anchors at %s, older than %d days; the "
                    "stride is counted from %s instead, which may shift its "
                    "phase. Re-anchor it on a recent eligible date.",
                    ds.id, anchor, MAX_ANCHOR_AGE_DAYS, floor,
                )
                start = min(start, floor)
            else:
                start = anchor
        return start

    async def async_refresh(self) -> None:
        """Recompute every day-set from today forward, in dependency order."""
        today = dt_util.now().date()
        start = self._window_start(today)
        days = (today + datetime.timedelta(days=HORIZON_DAYS) - start).days
        for cfg in self._ordered:
            day_set = self.day_sets[cfg["id"]]
            base_set = None
            if day_set.base_day_set and day_set.id not in self._unresolved:
                base = self.day_sets.get(day_set.base_day_set)
                base_set = set(base._eligible) if base else None
            await day_set.async_refresh(self.hass, start, days=days, base_set=base_set)
        async_dispatcher_send(self.hass, SIGNAL_DAY_SETS_UPDATED)

        # Boot ordering: a source calendar may not exist, or may exist but
        # not be serviceable, at the moment we run. The state-change listener
        # usually catches the first case; nothing reliably catches the second.
        # So if anything was missing, come back shortly and try again.
        missing = sorted({e for ds in self.day_sets.values() for e in ds._missing_sources})
        if self._retry_unsub:
            self._retry_unsub()
            self._retry_unsub = None
        if missing:
            _LOGGER.debug(
                "Retrying in %ss for source calendar(s) not ready: %s",
                MISSING_SOURCE_RETRY_SECONDS, missing,
            )
            self._retry_unsub = async_call_later(
                self.hass, MISSING_SOURCE_RETRY_SECONDS, self._handle_retry
            )

    async def _handle_retry(self, _now) -> None:
        self._retry_unsub = None
        await self.async_refresh()

    async def _handle_midnight(self, _now) -> None:
        await self.async_refresh()

    @callback
    def _handle_source_change(self, event) -> None:
        self.hass.async_create_task(self.async_refresh())

    async def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._retry_unsub:
            self._retry_unsub()
            self._retry_unsub = None
