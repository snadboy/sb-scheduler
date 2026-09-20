"""Constants for sb_scheduler."""

DOMAIN = "sb_scheduler"

# --- config entry -----------------------------------------------------------
CONF_DAY_SETS = "day_sets"
CONF_WORKDAY_CALENDAR = "workday_calendar"

# --- day-set fields ---------------------------------------------------------
CONF_ID = "id"
CONF_NAME = "name"
CONF_WEEKDAYS = "weekdays"
CONF_INVERT = "invert"
# Shifts the whole resolved set by N days: "the night before trash day" is
# Trash Day with offset_days = -1. Applied LAST, after force/veto/base and
# after invert, so it is always "that set, moved".
CONF_OFFSET_DAYS = "offset_days"
MAX_OFFSET_DAYS = 30
# Three tiers, because two could not express the real workday rule:
#   force  — wins outright (a days-off entry titled "Workday" reinstates one)
#   veto   — cancels an otherwise-eligible day
#   base   — what is eligible to begin with: a weekday mask and/or calendars
# `include_*` was the old name for what is really the BASE tier; it is still
# read so existing config keeps working.
CONF_BASE_CALENDARS = "base_calendars"
CONF_BASE_DATES = "base_dates"
CONF_FORCE_CALENDARS = "force_calendars"
CONF_FORCE_DATES = "force_dates"
CONF_FORCE_MATCH = "force_match"
CONF_EXCLUDE_CALENDARS = "exclude_calendars"
CONF_EXCLUDE_DATES = "exclude_dates"
CONF_EXCLUDE_MATCH = "exclude_match"
# legacy
CONF_INCLUDE_CALENDARS = "include_calendars"
CONF_INCLUDE_DATES = "include_dates"

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# How far ahead eligibility is precomputed. Generous on purpose: a school-year
# day-set has a ~10 week summer gap, and upstream's 16-day ceiling is exactly
# the bug this replaces. Cheap because it is one calendar query per source.
HORIZON_DAYS = 400

# Also compute a little into the PAST. A day-set is published as a calendar
# entity, and HA's calendar panel opens on the current month — so it is
# routinely asked about dates before today. Without this the entity shows an
# empty first half of the month and logs a warning per query.
PAST_DAYS = 45

# Refresh the window shortly after midnight, so "today" is always in range.
REFRESH_HOUR = 0
REFRESH_MINUTE = 5

SIGNAL_DAY_SETS_UPDATED = f"{DOMAIN}_day_sets_updated"
SIGNAL_SCHEDULES_UPDATED = f"{DOMAIN}_schedules_updated"

# --- schedules --------------------------------------------------------------
CONF_SCHEDULE_ID = "schedule_id"
CONF_DAY_SET = "day_set"
CONF_PATTERN = "pattern"
CONF_ENABLED = "enabled"
CONF_ACTIONS = "actions"

PATTERN_OCCURRENCES = "occurrences"
PATTERN_INTERVAL = "interval"

CONF_OCCURRENCES = "occurrences"
CONF_START = "start"
CONF_STOP = "stop"
CONF_EVERY_MINUTES = "every_minutes"

ATTR_NEXT_TRIGGER = "next_trigger"
# Matches the name HA automations use, so it reads the same everywhere.
ATTR_LAST_TRIGGERED = "last_triggered"

# Interval patterns expand to concrete times at arm-time. A cap keeps a typo
# such as every_minutes=1 over 24h from producing 1440 firings unnoticed.
MAX_INTERVAL_OCCURRENCES = 288

# --- carried over from scheduler-component's action engine ------------------
# actions.py is ported nearly verbatim, so it still expects these names.
ATTR_ACTIONS = "actions"
ATTR_VALUE = "value"
ATTR_CONDITION_TYPE = "condition_type"
CONDITION_TYPE_AND = "and"
ATTR_MATCH_TYPE = "match_type"
MATCH_TYPE_EQUAL = "is"
MATCH_TYPE_UNEQUAL = "not"
MATCH_TYPE_BELOW = "below"
MATCH_TYPE_ABOVE = "above"
ATTR_TRACK_CONDITIONS = "track_conditions"
STATE_INIT = "init"

EVENT_STARTED = f"{DOMAIN}_started"
EVENT_ACTION_FAILED = f"{DOMAIN}_action_failed"

CONF_NOTIFY_ON_FAILURE = "notify_on_failure"
DEFAULT_NOTIFY_ON_FAILURE = True


def notify_on_failure(hass) -> bool:
    """Whether a failed action should also raise a persistent notification."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if entries:
        return bool(
            entries[0].options.get(CONF_NOTIFY_ON_FAILURE, DEFAULT_NOTIFY_ON_FAILURE)
        )
    return DEFAULT_NOTIFY_ON_FAILURE
