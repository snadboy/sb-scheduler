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

# A day-set can be BUILT ON another day-set instead of (or as well as) a mask
# and calendars. That is what keeps "every other Tuesday" and "Election Day"
# from each needing their own calendar source: they derive from Tuesday and
# Monday. Evaluated in dependency order; a cycle is an error, not a hang.
CONF_BASE_DAY_SET = "base_day_set"

# PICK narrows the eligible dates to a cadence or an ordinal:
#   none          — every eligible date (the default, and the old behaviour)
#   every         — every Nth eligible date, counting from an anchor date
#   nth_of_month  — the Nth (or last) eligible date of each calendar month
# Both stride over ELIGIBLE dates, not calendar days, so "every other trash
# day" follows a holiday shift and "last workday of the month" is holiday-aware.
CONF_PICK = "pick"
PICK_NONE = "none"
PICK_EVERY = "every"
PICK_NTH_OF_MONTH = "nth_of_month"
CONF_PICK_EVERY = "pick_every"
CONF_PICK_ANCHOR = "pick_anchor"
CONF_PICK_NTH = "pick_nth"
PICK_NTH_LAST = "last"
PICK_NTH_OPTIONS = ["1", "2", "3", "4", "5", PICK_NTH_LAST]

# Keep only dates in these months (1-12). Applied after pick and before
# invert, which is what makes "first Monday of November" come out right
# rather than "first Monday among November's Mondays" (same thing) or
# "first Monday of the year that lands in November" (not).
CONF_MONTHS = "months"

# The calendar entity is what a derived set does NOT need — nobody wants a
# calendar per special case. Default on so nothing existing changes.
CONF_EXPOSE_CALENDAR = "expose_calendar"

# An "every N" anchor can be older than PAST_DAYS. The window is extended back
# to the anchor so the stride can be counted from it, but not without limit.
MAX_ANCHOR_AGE_DAYS = 3 * 366

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

# When a refresh finds a source calendar not created or not serviceable yet
# (boot ordering), try again this many seconds later — deterministically,
# rather than hoping the source emits another state change.
MISSING_SOURCE_RETRY_SECONDS = 20

SIGNAL_DAY_SETS_UPDATED = f"{DOMAIN}_day_sets_updated"
SIGNAL_SCHEDULES_UPDATED = f"{DOMAIN}_schedules_updated"

# --- schedules --------------------------------------------------------------
CONF_SCHEDULE_ID = "schedule_id"
CONF_DAY_SET = "day_set"
# Run on every day that is NOT in the day-set. This is the schedule's one
# transformation of a day-set — it means "Non-workday" no longer needs to
# exist as its own inverted set (and its own calendar) just to be picked.
CONF_NEGATE = "negate"
CONF_PATTERN = "pattern"
CONF_ENABLED = "enabled"
CONF_ACTIONS = "actions"
# A schedule is a day-set plus one or more STEPS. A step pairs a time pattern
# with the actions to run at it, so "irrigation on at 06:00, off at 07:00" is
# one schedule — and its day-set is stated once, where it cannot drift.
CONF_STEPS = "steps"
CONF_STEP_ID = "step_id"

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
