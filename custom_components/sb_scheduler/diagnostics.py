"""Diagnostics: the whole configuration as one downloadable JSON.

The config entry lives in HA's .storage — backed up, UI-editable, but not
something to diff or keep in git. This is the export: Settings → Integrations
→ SB Scheduler → Download diagnostics.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_DAY_SETS, DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    out: dict = {
        "day_sets": list(entry.options.get(CONF_DAY_SETS, [])),
        "schedules": list(data.schedules.schedules.values()) if data else [],
    }
    if data:
        out["resolved"] = {
            ds.id: {
                "window": [w.isoformat() for w in ds._window] if ds._window else None,
                "eligible_count": len(ds._eligible),
                "missing_sources": ds._missing_sources,
            }
            for ds in data.day_sets.day_sets.values()
        }
    return out
