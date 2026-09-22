"""The one device every entity hangs off.

There is no hardware, so this is a SERVICE device (HA's idiom for a virtual
device: different icon, no "delete device" affordance). One per config entry,
keyed on the entry id, so calendars, schedule switches, the roster and the
refresh button all share a device page.

Naming (HA 2026.9, read in the container): every entity on a device gets
the friendly name "<device> <entity>" — `has_entity_name` no longer opts
out, it only controls whether a device prefix already in the entity's name
is stripped first. So the device is deliberately named just "SB": short
enough that "SB Workday", "SB Day Off", "SB Garden Lights" read as labels
rather than sentences, and it groups our calendars apart from the user's
own in the Calendar panel (there were two bare "Trash Day" rows before).
Entities therefore carry ONLY their own noun ("Refresh", "Day types") —
never repeat the prefix in `_attr_name`.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN


def device_info(entry: ConfigEntry, version) -> DeviceInfo:
    # `integration.version` is an AwesomeVersion; the device registry compares
    # sw_version as a str and raises on the mismatch. Verified the hard way.
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        entry_type=DeviceEntryType.SERVICE,
        name="SB",
        manufacturer="snadboy",
        model="Day types and schedules",
        sw_version=str(version) if version else None,
    )
