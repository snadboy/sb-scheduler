"""Config and options flow.

Day-sets are defined here rather than in YAML — the whole point is that adding
"School Day" is a visual act, not a file edit.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.util import slugify
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
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
    CONF_BASE_CALENDARS,
    CONF_BASE_DATES,
    CONF_BASE_DAY_SET,
    CONF_DAY_SETS,
    CONF_EXCLUDE_CALENDARS,
    CONF_EXCLUDE_DATES,
    CONF_EXCLUDE_MATCH,
    CONF_EXPOSE_CALENDAR,
    CONF_FORCE_CALENDARS,
    CONF_FORCE_DATES,
    CONF_FORCE_MATCH,
    CONF_ID,
    CONF_INVERT,
    CONF_MONTHS,
    CONF_NAME,
    CONF_OFFSET_DAYS,
    CONF_PICK,
    CONF_PICK_ANCHOR,
    CONF_PICK_EVERY,
    CONF_PICK_NTH,
    CONF_WEEKDAYS,
    CONF_WORKDAY_CALENDAR,
    DOMAIN,
    MAX_OFFSET_DAYS,
    PICK_EVERY,
    PICK_NONE,
    PICK_NTH_OF_MONTH,
    PICK_NTH_OPTIONS,
    WEEKDAYS,
)
from .day_set import allocate_day_set_id, validate_day_set

CALENDARS = EntitySelector(EntitySelectorConfig(domain="calendar", multiple=True))
DATES = TextSelector(
    TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
)
WEEKDAY_PICKER = SelectSelector(
    SelectSelectorConfig(
        options=WEEKDAYS, multiple=True, mode=SelectSelectorMode.LIST, translation_key="weekdays"
    )
)


TEXT = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
OFFSET = NumberSelector(
    NumberSelectorConfig(
        min=-MAX_OFFSET_DAYS, max=MAX_OFFSET_DAYS, step=1,
        mode=NumberSelectorMode.BOX,
    )
)
EVERY = NumberSelector(
    NumberSelectorConfig(min=1, max=366, step=1, mode=NumberSelectorMode.BOX)
)
PICK_SELECT = SelectSelector(
    SelectSelectorConfig(
        options=[PICK_NONE, PICK_EVERY, PICK_NTH_OF_MONTH],
        mode=SelectSelectorMode.DROPDOWN, translation_key="pick",
    )
)
NTH_SELECT = SelectSelector(
    SelectSelectorConfig(
        options=PICK_NTH_OPTIONS, mode=SelectSelectorMode.DROPDOWN,
        translation_key="pick_nth",
    )
)
MONTHS_SELECT = SelectSelector(
    SelectSelectorConfig(
        options=[str(m) for m in range(1, 13)], multiple=True,
        mode=SelectSelectorMode.DROPDOWN, translation_key="months",
    )
)
ANCHOR = DateSelector()

# The dropdown needs a "none" entry; the stored value for none is "".
NO_BASE = "__none__"

# The form is FIVE collapsible sections, because twenty flat fields is a wall.
# Stored config stays flat; these lists are what flatten() folds back in.
SECTIONS: dict[str, list[str]] = {
    "sources": [CONF_WEEKDAYS, CONF_BASE_DAY_SET, CONF_BASE_CALENDARS, CONF_BASE_DATES],
    "cancelled": [CONF_EXCLUDE_CALENDARS, CONF_EXCLUDE_DATES, CONF_EXCLUDE_MATCH],
    "always": [CONF_FORCE_CALENDARS, CONF_FORCE_DATES, CONF_FORCE_MATCH],
    "pick": [CONF_PICK, CONF_PICK_EVERY, CONF_PICK_ANCHOR, CONF_PICK_NTH, CONF_MONTHS],
    "advanced": [CONF_INVERT, CONF_OFFSET_DAYS, CONF_EXPOSE_CALENDAR],
}


def flatten(user_input: dict) -> dict:
    """Fold a sectioned form submission into the flat shape that is stored."""
    flat: dict = {CONF_NAME: user_input.get(CONF_NAME, "")}
    for sec in SECTIONS:
        flat.update(user_input.get(sec) or {})
    if flat.get(CONF_BASE_DAY_SET) == NO_BASE:
        flat[CONF_BASE_DAY_SET] = ""
    for key in (CONF_PICK_EVERY, CONF_OFFSET_DAYS):
        if key in flat and flat[key] is not None:
            flat[key] = int(flat[key])
    return flat


def day_set_schema(defaults: dict | None, others: list[dict]) -> vol.Schema:
    """The one form used for both add and edit.

    Sections read as the precedence chain -- SOURCES (what is eligible), then
    CANCELLED, then ALWAYS -- followed by PICK (cadence / ordinal / months)
    and ADVANCED. A section starts collapsed unless it already holds a value,
    so an existing day-set opens showing exactly what it uses.

    `others` are the day-sets this one may build on (never itself).
    """
    d = defaults or {}
    # Legacy config used include_* for what is now the base tier.
    base_cals = d.get(CONF_BASE_CALENDARS, d.get("include_calendars", []))
    base_dates = d.get(CONF_BASE_DATES, d.get("include_dates", ""))

    def has(*keys) -> bool:
        return any(d.get(k) for k in keys)

    base_picker = SelectSelector(
        SelectSelectorConfig(
            options=[{"value": NO_BASE, "label": "—"}]
            + [{"value": o[CONF_ID], "label": o[CONF_NAME]} for o in others],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )

    sources = {
        vol.Optional(CONF_WEEKDAYS, default=d.get(CONF_WEEKDAYS, [])): WEEKDAY_PICKER,
        vol.Optional(
            CONF_BASE_DAY_SET, default=d.get(CONF_BASE_DAY_SET) or NO_BASE
        ): base_picker,
        vol.Optional(CONF_BASE_CALENDARS, default=base_cals): CALENDARS,
        vol.Optional(CONF_BASE_DATES, default=base_dates): DATES,
    }
    cancelled = {
        vol.Optional(
            CONF_EXCLUDE_CALENDARS, default=d.get(CONF_EXCLUDE_CALENDARS, [])
        ): CALENDARS,
        vol.Optional(CONF_EXCLUDE_DATES, default=d.get(CONF_EXCLUDE_DATES, "")): DATES,
        vol.Optional(CONF_EXCLUDE_MATCH, default=d.get(CONF_EXCLUDE_MATCH, "")): TEXT,
    }
    always = {
        vol.Optional(
            CONF_FORCE_CALENDARS, default=d.get(CONF_FORCE_CALENDARS, [])
        ): CALENDARS,
        vol.Optional(CONF_FORCE_DATES, default=d.get(CONF_FORCE_DATES, "")): DATES,
        vol.Optional(CONF_FORCE_MATCH, default=d.get(CONF_FORCE_MATCH, "")): TEXT,
    }
    # A DateSelector rejects "" as a default, so the anchor only gets one
    # when there is a real date to show.
    anchor_key = (
        vol.Optional(CONF_PICK_ANCHOR, default=d[CONF_PICK_ANCHOR])
        if d.get(CONF_PICK_ANCHOR) else vol.Optional(CONF_PICK_ANCHOR)
    )
    pick = {
        vol.Optional(CONF_PICK, default=d.get(CONF_PICK) or PICK_NONE): PICK_SELECT,
        vol.Optional(CONF_PICK_EVERY, default=d.get(CONF_PICK_EVERY) or 2): EVERY,
        anchor_key: ANCHOR,
        vol.Optional(CONF_PICK_NTH, default=str(d.get(CONF_PICK_NTH) or "1")): NTH_SELECT,
        vol.Optional(
            CONF_MONTHS, default=[str(m) for m in (d.get(CONF_MONTHS) or [])]
        ): MONTHS_SELECT,
    }
    advanced = {
        vol.Optional(CONF_INVERT, default=d.get(CONF_INVERT, False)): BooleanSelector(),
        vol.Optional(CONF_OFFSET_DAYS, default=d.get(CONF_OFFSET_DAYS, 0)): OFFSET,
        vol.Optional(
            CONF_EXPOSE_CALENDAR, default=d.get(CONF_EXPOSE_CALENDAR, True)
        ): BooleanSelector(),
    }

    picking = (d.get(CONF_PICK) or PICK_NONE) != PICK_NONE or has(CONF_MONTHS)
    tweaked = has(CONF_INVERT, CONF_OFFSET_DAYS) or d.get(CONF_EXPOSE_CALENDAR) is False
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=d.get(CONF_NAME, "")): str,
            vol.Required("sources"): section(vol.Schema(sources), {"collapsed": False}),
            vol.Required("cancelled"): section(
                vol.Schema(cancelled),
                {"collapsed": not has(CONF_EXCLUDE_CALENDARS, CONF_EXCLUDE_DATES)},
            ),
            vol.Required("always"): section(
                vol.Schema(always),
                {"collapsed": not has(CONF_FORCE_CALENDARS, CONF_FORCE_DATES)},
            ),
            vol.Required("pick"): section(vol.Schema(pick), {"collapsed": not picking}),
            vol.Required("advanced"): section(vol.Schema(advanced), {"collapsed": not tweaked}),
        }
    )


# Validation lives in day_set.py so the set_day_set service (the card's write
# path) and this form can never disagree about what is allowed.
_validate = validate_day_set


def _seed_day_sets(workday_calendar: str | None,
                   days_off_calendar: str | None = None) -> list[dict]:
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
        # Base = the workday calendar; PTO vetoes it; a days-off entry titled
        # "Workday" reinstates it. Wired only if a days-off calendar exists.
        shape = {CONF_BASE_CALENDARS: [workday_calendar]}
        if days_off_calendar:
            shape[CONF_EXCLUDE_CALENDARS] = [days_off_calendar]
            shape[CONF_FORCE_CALENDARS] = [days_off_calendar]
            shape[CONF_FORCE_MATCH] = "Workday"
        day_sets += [
            {CONF_ID: "workday", CONF_NAME: "Workday", **shape},
            {CONF_ID: "non_workday", CONF_NAME: "Non-workday", **shape,
             CONF_INVERT: True},
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
                        user_input.get(CONF_WORKDAY_CALENDAR),
                        user_input.get("days_off_calendar"),
                    )
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_WORKDAY_CALENDAR): EntitySelector(
                        EntitySelectorConfig(domain="calendar")
                    ),
                    vol.Optional("days_off_calendar"): EntitySelector(
                        EntitySelectorConfig(domain="calendar")
                    ),
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
        return allocate_day_set_id(name, {d[CONF_ID] for d in self._day_sets}, slugify)

    async def async_step_add(self, user_input=None):
        errors: dict[str, str] = {}
        flat: dict | None = None
        if user_input is not None:
            flat = flatten(user_input)
            errors = _validate(flat, self._day_sets, None)
            if not errors:
                new = dict(flat)
                new[CONF_ID] = self._new_id(flat[CONF_NAME])
                return self._save([*self._day_sets, new])
        return self.async_show_form(
            step_id="add",
            data_schema=day_set_schema(flat, self._day_sets),
            errors=errors,
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
        flat: dict | None = None
        others = [d for d in self._day_sets if d[CONF_ID] != self._editing]
        if user_input is not None:
            flat = flatten(user_input)
            errors = _validate(flat, self._day_sets, self._editing)
            if not errors:
                updated = {**current, **flat}
                return self._save(
                    [
                        updated if d[CONF_ID] == self._editing else d
                        for d in self._day_sets
                    ]
                )
        return self.async_show_form(
            step_id="edit",
            data_schema=day_set_schema(flat or current, others),
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
