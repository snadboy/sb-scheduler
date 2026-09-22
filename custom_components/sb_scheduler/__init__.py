"""sb_scheduler — calendar-aware scheduling.

Phase 0 gave day-sets: named groups of DATES, answerable for any date rather
than only for now. Phase 1 adds schedules on top: pick one day-set and one time
pattern. Execution (queueing, availability retries, conditions) is inherited
from scheduler-component; the scheduling core is not.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import slugify

from .const import (
    CONF_ACTIONS,
    CONF_BASE_DAY_SET,
    CONF_DAY_SET,
    CONF_DAY_SETS,
    CONF_ENABLED,
    CONF_ID,
    CONF_NAME,
    CONF_NEGATE,
    CONF_PATTERN,
    CONF_SCHEDULE_ID,
    CONF_STEPS,
    DOMAIN,
    SIGNAL_SCHEDULES_UPDATED,
)
from .day_set import (
    DAY_SET_FIELDS,
    VALIDATION_MESSAGES,
    allocate_day_set_id,
    validate_day_set,
)
from .registry import DaySetRegistry
from .schedule_store import ScheduleStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["calendar", "switch", "sensor"]

SERVICE_QUERY = "query_day_set"
SERVICE_REFRESH = "refresh"
SERVICE_CREATE = "create_schedule"
SERVICE_EDIT = "edit_schedule"
SERVICE_REMOVE = "remove_schedule"
SERVICE_SET_DAY_SET = "set_day_set"
SERVICE_REMOVE_DAY_SET = "remove_day_set"

# The card's write path for day-sets. Permissive on purpose: the known fields
# are filtered out afterwards, and validate_day_set decides what is allowed,
# so this stays in step with the options form without a second schema.
DAY_SET_SCHEMA = vol.Schema(
    {vol.Optional(CONF_ID): cv.string, vol.Required(CONF_NAME): cv.string},
    extra=vol.ALLOW_EXTRA,
)
REMOVE_DAY_SET_SCHEMA = vol.Schema({vol.Required(CONF_ID): cv.string})

QUERY_SCHEMA = vol.Schema(
    {vol.Required("day_set"): cv.string, vol.Optional("date"): cv.date}
)

SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional("name"): cv.string,
        vol.Optional(CONF_DAY_SET): cv.string,
        vol.Optional(CONF_NEGATE): cv.boolean,
        # Either steps, or a legacy single pattern+actions which is migrated
        # into one implicit step by the store.
        vol.Optional(CONF_STEPS): list,
        vol.Optional(CONF_PATTERN): dict,
        vol.Optional(CONF_ACTIONS): list,
        vol.Optional("conditions"): list,
        vol.Optional("condition_type"): cv.string,
        vol.Optional("track_conditions"): cv.boolean,
        vol.Optional(CONF_ENABLED): cv.boolean,
    }
)
EDIT_SCHEMA = SCHEDULE_SCHEMA.extend({vol.Required(CONF_SCHEDULE_ID): cv.string})
REMOVE_SCHEMA = vol.Schema({vol.Required(CONF_SCHEDULE_ID): cv.string})


@dataclass
class RuntimeData:
    """Everything one config entry owns."""

    day_sets: DaySetRegistry
    schedules: ScheduleStore


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up day-sets and schedules for this entry."""
    day_sets = DaySetRegistry(hass, dict(entry.options))
    await day_sets.async_setup()

    schedules = ScheduleStore(hass)
    await schedules.async_load()

    data = RuntimeData(day_sets=day_sets, schedules=schedules)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = data

    _purge_orphaned_entities(hass, entry, data)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_options_updated))
    _register_services(hass)

    _LOGGER.debug(
        "Set up %d day-set(s) and %d schedule(s)",
        len(day_sets.day_sets),
        len(schedules.schedules),
    )
    return True


@callback
def _purge_orphaned_entities(
    hass: HomeAssistant, entry: ConfigEntry, data: RuntimeData
) -> None:
    """Drop entities for day-sets or schedules that no longer exist.

    HA only cleans the registry when the whole config entry goes away, so
    deleting a day-set would otherwise leave its entity behind and the next one
    with that name would land on `_2`.

    Both kinds of unique_id must be listed here. Listing only day-sets would
    delete every schedule switch on the next restart.
    """
    entity_registry = er.async_get(hass)
    # A day-set that has switched its calendar OFF must drop the entity too —
    # otherwise it lingers as a `restored` orphan, which is the exact symptom
    # the ha-orphans skill exists to chase.
    valid = {
        f"{entry.entry_id}_{ds.id}"
        for ds in data.day_sets.day_sets.values()
        if ds.expose_calendar
    }
    valid |= {
        f"{entry.entry_id}_schedule_{sid}" for sid in data.schedules.schedules
    }
    valid.add(f"{entry.entry_id}_day_sets")   # the roster sensor
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if entity.unique_id not in valid:
            _LOGGER.debug("Removing orphaned entity %s", entity.entity_id)
            entity_registry.async_remove(entity.entity_id)


def _entries(hass: HomeAssistant) -> list[RuntimeData]:
    return list(hass.data.get(DOMAIN, {}).values())


def _register_services(hass: HomeAssistant) -> None:
    """Register once, regardless of how many entries exist."""
    if hass.services.has_service(DOMAIN, SERVICE_QUERY):
        return

    async def async_query(call: ServiceCall) -> dict:
        """Answer is-eligible / next-date for a day-set, for any date."""
        day = call.data.get("date") or datetime.date.today()
        for data in _entries(hass):
            day_set = data.day_sets.get(call.data["day_set"])
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
        for data in _entries(hass):
            await data.day_sets.async_refresh("service")

    async def async_create(call: ServiceCall) -> dict:
        data = _entries(hass)[0]
        if call.data.get(CONF_DAY_SET) and not data.day_sets.get(call.data[CONF_DAY_SET]):
            raise vol.Invalid(f"Unknown day-set '{call.data[CONF_DAY_SET]}'")
        schedule = data.schedules.async_create(dict(call.data))
        async_dispatcher_send(hass, SIGNAL_SCHEDULES_UPDATED)
        return {CONF_SCHEDULE_ID: schedule[CONF_SCHEDULE_ID], "name": schedule["name"]}

    async def async_edit(call: ServiceCall) -> None:
        changes = {k: v for k, v in call.data.items() if k != CONF_SCHEDULE_ID}
        for data in _entries(hass):
            if data.schedules.async_update(call.data[CONF_SCHEDULE_ID], changes):
                async_dispatcher_send(hass, SIGNAL_SCHEDULES_UPDATED)
                return
        raise vol.Invalid(f"Unknown schedule '{call.data[CONF_SCHEDULE_ID]}'")

    async def async_remove(call: ServiceCall) -> None:
        for data in _entries(hass):
            if data.schedules.async_delete(call.data[CONF_SCHEDULE_ID]):
                async_dispatcher_send(hass, SIGNAL_SCHEDULES_UPDATED)
                # The entity goes on the next reload; drop it now so the name frees up.
                for entry in hass.config_entries.async_entries(DOMAIN):
                    _purge_orphaned_entities(hass, entry, data)
                return
        raise vol.Invalid(f"Unknown schedule '{call.data[CONF_SCHEDULE_ID]}'")

    # --- day-sets: the config entry's options, edited from the card --------
    def _entry() -> ConfigEntry:
        return hass.config_entries.async_entries(DOMAIN)[0]

    async def async_set_day_set(call: ServiceCall) -> dict:
        """Create (no id) or update (id) a day-set. Saving the options
        reloads the entry, so the change is live within a couple of seconds."""
        entry = _entry()
        day_sets = list(entry.options.get(CONF_DAY_SETS, []))
        flat = {k: v for k, v in call.data.items() if k in DAY_SET_FIELDS}
        editing = call.data.get(CONF_ID) or None
        if editing and not any(d[CONF_ID] == editing for d in day_sets):
            raise ServiceValidationError(f"Unknown day-set '{editing}'")
        errors = validate_day_set(flat, day_sets, editing)
        if errors:
            code = errors.get("base") or next(iter(errors.values()))
            raise ServiceValidationError(VALIDATION_MESSAGES.get(code, code))
        if editing:
            day_sets = [{**d, **flat} if d[CONF_ID] == editing else d for d in day_sets]
            ds_id = editing
        else:
            ds_id = allocate_day_set_id(flat[CONF_NAME], {d[CONF_ID] for d in day_sets}, slugify)
            day_sets.append({**flat, CONF_ID: ds_id})
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_DAY_SETS: day_sets}
        )
        return {CONF_ID: ds_id}

    async def async_remove_day_set(call: ServiceCall) -> None:
        """Refuse while anything depends on it. A silently broken schedule is
        the failure mode this whole integration exists to avoid."""
        entry = _entry()
        ds_id = call.data[CONF_ID]
        day_sets = list(entry.options.get(CONF_DAY_SETS, []))
        if not any(d[CONF_ID] == ds_id for d in day_sets):
            raise ServiceValidationError(f"Unknown day-set '{ds_id}'")
        users: list[str] = []
        for data in _entries(hass):
            users += [f"schedule '{s.get('name')}'" for s in data.schedules.schedules.values()
                      if s.get(CONF_DAY_SET) == ds_id]
        users += [f"day-set '{d.get(CONF_NAME)}'" for d in day_sets
                  if d.get(CONF_BASE_DAY_SET) == ds_id]
        if users:
            raise ServiceValidationError(
                f"'{ds_id}' is still used by {', '.join(users)}. Change those first."
            )
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_DAY_SETS: [d for d in day_sets if d[CONF_ID] != ds_id]}
        )

    hass.services.async_register(
        DOMAIN, SERVICE_SET_DAY_SET, async_set_day_set, schema=DAY_SET_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_DAY_SET, async_remove_day_set, schema=REMOVE_DAY_SET_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN, SERVICE_QUERY, async_query, schema=QUERY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(DOMAIN, SERVICE_REFRESH, async_refresh)
    hass.services.async_register(
        DOMAIN, SERVICE_CREATE, async_create, schema=SCHEDULE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, SERVICE_EDIT, async_edit, schema=EDIT_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE, async_remove, schema=REMOVE_SCHEMA
    )


async def async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Day-sets changed — reload so entities match the new configuration."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        data: RuntimeData = hass.data[DOMAIN].pop(entry.entry_id)
        await data.day_sets.async_unload()
        if not hass.data[DOMAIN]:
            for service in (
                SERVICE_QUERY, SERVICE_REFRESH, SERVICE_CREATE,
                SERVICE_EDIT, SERVICE_REMOVE,
                SERVICE_SET_DAY_SET, SERVICE_REMOVE_DAY_SET,
            ):
                hass.services.async_remove(DOMAIN, service)
    return unloaded
