"""Config and options flow.

Day-sets are defined here rather than in YAML — the whole point is that adding
"School Day" is a visual act, not a file edit.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.util import slugify
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_DAY_SETS,
    CONF_EXCLUDE_CALENDARS,
    CONF_EXCLUDE_DATES,
    CONF_ID,
    CONF_INCLUDE_CALENDARS,
    CONF_INCLUDE_DATES,
    CONF_INVERT,
    CONF_NAME,
    CONF_WEEKDAYS,
    CONF_WORKDAY_CALENDAR,
    DOMAIN,
    WEEKDAYS,
)
from .day_set import InvalidDateSpec, parse_date_spec

CALENDARS = EntitySelector(EntitySelectorConfig(domain="calendar", multiple=True))
DATES = TextSelector(
    TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
)
WEEKDAY_PICKER = SelectSelector(
    SelectSelectorConfig(
        options=WEEKDAYS, multiple=True, mode=SelectSelectorMode.LIST, translation_key="weekdays"
    )
)


def day_set_schema(defaults: dict | None = None) -> vol.Schema:
    """The one form used for both add and edit."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=d.get(CONF_NAME, "")): str,
            vol.Optional(
                CONF_WEEKDAYS, default=d.get(CONF_WEEKDAYS, [])
            ): WEEKDAY_PICKER,
            vol.Optional(
                CONF_EXCLUDE_CALENDARS, default=d.get(CONF_EXCLUDE_CALENDARS, [])
            ): CALENDARS,
            vol.Optional(
                CONF_EXCLUDE_DATES, default=d.get(CONF_EXCLUDE_DATES, "")
            ): DATES,
            vol.Optional(
                CONF_INCLUDE_CALENDARS, default=d.get(CONF_INCLUDE_CALENDARS, [])
            ): CALENDARS,
            vol.Optional(
                CONF_INCLUDE_DATES, default=d.get(CONF_INCLUDE_DATES, "")
            ): DATES,
            vol.Optional(CONF_INVERT, default=d.get(CONF_INVERT, False)): BooleanSelector(),
        }
    )


def _validate_dates(user_input: dict) -> dict[str, str]:
    """Hand-typed dates are the most likely thing to be wrong. Say which."""
    errors: dict[str, str] = {}
    for key in (CONF_INCLUDE_DATES, CONF_EXCLUDE_DATES):
        try:
            parse_date_spec(user_input.get(key) or "")
        except InvalidDateSpec:
            errors[key] = "invalid_dates"
    return errors


def _seed_day_sets(workday_calendar: str | None) -> list[dict]:
    """Start with something usable rather than an empty list."""
    day_sets = [
        {
            CONF_ID: "daily",
            CONF_NAME: "Daily",
            CONF_WEEKDAYS: list(WEEKDAYS),
        },
        {
            CONF_ID: "weekend",
            CONF_NAME: "Weekend",
            # Literally Saturday and Sunday. "Non-workday" is its own thing --
            # conflating the two is the wart this project exists to remove.
            CONF_WEEKDAYS: ["sat", "sun"],
        },
    ]
    if workday_calendar:
        day_sets += [
            {
                CONF_ID: "workday",
                CONF_NAME: "Workday",
                CONF_INCLUDE_CALENDARS: [workday_calendar],
            },
            {
                CONF_ID: "non_workday",
                CONF_NAME: "Non-workday",
                CONF_INCLUDE_CALENDARS: [workday_calendar],
                CONF_INVERT: True,
            },
        ]
    return day_sets


class SbSchedulerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Single-instance setup."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(
                title="SB Scheduler",
                data={},
                options={
                    CONF_DAY_SETS: _seed_day_sets(
                        user_input.get(CONF_WORKDAY_CALENDAR)
                    )
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_WORKDAY_CALENDAR): EntitySelector(
                        EntitySelectorConfig(domain="calendar")
                    )
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SbSchedulerOptionsFlow()


class SbSchedulerOptionsFlow(config_entries.OptionsFlow):
    """Add, edit and remove day-sets."""

    def __init__(self) -> None:
        self._editing: str | None = None

    @property
    def _day_sets(self) -> list[dict]:
        return list(self.config_entry.options.get(CONF_DAY_SETS, []))

    def _save(self, day_sets: list[dict]):
        return self.async_create_entry(data={CONF_DAY_SETS: day_sets})

    async def async_step_init(self, user_input=None):
        menu = ["add"]
        if self._day_sets:
            menu += ["pick_edit", "pick_remove"]
        return self.async_show_menu(step_id="init", menu_options=menu)

    # --- add ---------------------------------------------------------------
    def _new_id(self, name: str) -> str:
        """A readable id derived from the name.

        Schedules will reference day-sets by id, so an opaque token would end up
        in every schedule's config and in every service call. "school_day" beats
        "e94e9983".
        """
        base = slugify(name) or "day_set"
        taken = {d[CONF_ID] for d in self._day_sets}
        if base not in taken:
            return base
        n = 2
        while f"{base}_{n}" in taken:
            n += 1
        return f"{base}_{n}"

    async def async_step_add(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_dates(user_input)
            if not errors:
                new = dict(user_input)
                new[CONF_ID] = self._new_id(user_input[CONF_NAME])
                return self._save([*self._day_sets, new])
        return self.async_show_form(
            step_id="add", data_schema=day_set_schema(user_input), errors=errors
        )

    # --- edit --------------------------------------------------------------
    async def async_step_pick_edit(self, user_input=None):
        if user_input is not None:
            self._editing = user_input[CONF_ID]
            return await self.async_step_edit()
        return self.async_show_form(
            step_id="pick_edit", data_schema=self._picker()
        )

    async def async_step_edit(self, user_input=None):
        current = next(
            (d for d in self._day_sets if d[CONF_ID] == self._editing), None
        )
        if current is None:
            return self.async_abort(reason="unknown_day_set")

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_dates(user_input)
            if not errors:
                updated = {**current, **user_input}
                return self._save(
                    [
                        updated if d[CONF_ID] == self._editing else d
                        for d in self._day_sets
                    ]
                )
        return self.async_show_form(
            step_id="edit",
            data_schema=day_set_schema(user_input or current),
            errors=errors,
            description_placeholders={"name": current[CONF_NAME]},
        )

    # --- remove ------------------------------------------------------------
    async def async_step_pick_remove(self, user_input=None):
        if user_input is not None:
            return self._save(
                [d for d in self._day_sets if d[CONF_ID] != user_input[CONF_ID]]
            )
        return self.async_show_form(
            step_id="pick_remove", data_schema=self._picker()
        )

    def _picker(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": d[CONF_ID], "label": d[CONF_NAME]}
                            for d in self._day_sets
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
