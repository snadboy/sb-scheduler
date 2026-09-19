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
CONF_INCLUDE_CALENDARS = "include_calendars"
CONF_EXCLUDE_CALENDARS = "exclude_calendars"
CONF_INCLUDE_DATES = "include_dates"
CONF_EXCLUDE_DATES = "exclude_dates"

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# How far ahead eligibility is precomputed. Generous on purpose: a school-year
# day-set has a ~10 week summer gap, and upstream's 16-day ceiling is exactly
# the bug this replaces. Cheap because it is one calendar query per source.
HORIZON_DAYS = 400

# Refresh the window shortly after midnight, so "today" is always in range.
REFRESH_HOUR = 0
REFRESH_MINUTE = 5

SIGNAL_DAY_SETS_UPDATED = f"{DOMAIN}_day_sets_updated"
