"""sb_scheduler — Phase 0: the day-set library.

Nothing here schedules anything yet. It publishes day-sets as calendar entities
and exposes a service to query them, which is deliberately useful on its own:
automations and dashboards can consume day-sets whether or not the scheduler
half ever ships.
"""

from __future__ import annotations

import datetime
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .registry import DaySetRegistry

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["calendar"]

SERVICE_QUERY = "query_day_set"
SERVICE_REFRESH = "refresh"

QUERY_SCHEMA = vol.Schema(
    {
        vol.Required("day_set"): cv.string,
        vol.Optional("date"): cv.date,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a day-set registry for this entry."""
    registry = DaySetRegistry(hass, dict(entry.options))
    await registry.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = registry
    _purge_orphaned_entities(hass, entry, registry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_options_updated))
    _register_services(hass)

    _LOGGER.debug("Set up %d day-set(s)", len(registry.day_sets))
    return True


@callback
def _purge_orphaned_entities(
    hass: HomeAssistant, entry: ConfigEntry, registry: DaySetRegistry
) -> None:
    """Drop calendar entities for day-sets that no longer exist.

    HA only cleans the registry when the whole config entry goes away, so
    deleting a day-set would otherwise leave its entity behind — and the next
    day-set with the same name would land on `_2`.
    """
    entity_registry = er.async_get(hass)
    valid = {f"{entry.entry_id}_{ds_id}" for ds_id in registry.day_sets}
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if entity.unique_id not in valid:
            _LOGGER.debug("Removing orphaned day-set entity %s", entity.entity_id)
            entity_registry.async_remove(entity.entity_id)


def _register_services(hass: HomeAssistant) -> None:
    """Register once, regardless of how many entries exist."""
    if hass.services.has_service(DOMAIN, SERVICE_QUERY):
        return

    async def async_query(call: ServiceCall) -> dict:
        """Answer is-eligible / next-date for a day-set, for any date."""
        day = call.data.get("date") or datetime.date.today()
        for registry in hass.data.get(DOMAIN, {}).values():
            day_set = registry.get(call.data["day_set"])
            if day_set is None:
                continue
            next_date = day_set.next_date_on_or_after(day)
            return {
                "day_set": day_set.id,
                "name": day_set.name,
                "date": day.isoformat(),
                "eligible": day_set.is_eligible(day),
                "next_date": next_date.isoformat() if next_date else None,
            }
        raise vol.Invalid(f"Unknown day-set '{call.data['day_set']}'")

    async def async_refresh(_call: ServiceCall) -> None:
        for registry in hass.data.get(DOMAIN, {}).values():
            await registry.async_refresh()

    hass.services.async_register(
        DOMAIN,
        SERVICE_QUERY,
        async_query,
        schema=QUERY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(DOMAIN, SERVICE_REFRESH, async_refresh)


async def async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Day-sets changed — reload so entities match the new configuration."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        registry: DaySetRegistry = hass.data[DOMAIN].pop(entry.entry_id)
        await registry.async_unload()
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_QUERY)
            hass.services.async_remove(DOMAIN, SERVICE_REFRESH)
    return unloaded
