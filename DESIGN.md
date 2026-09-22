# sb_scheduler — design

**Status:** Phases 0 and 1 implemented and verified live 2026-09-19. Phase 2 (the card) next.
**One-line goal:** one visual place for every time-based rule — "do X at time T on day-set S".

## Why not keep extending scheduler-component

`nielsfaber/scheduler-component` has the right *execution* engine and the wrong
*scheduling* model. Its day vocabulary is a closed enum (`daily`, `mon`…`sun`,
`workday`, `weekend`), where `workday`/`weekend` are special-cased to a single
hardcoded `binary_sensor`. Three hard limits follow, all verified in the code:

| Limit | Where | Consequence |
|---|---|---|
| Day vocabulary is a fixed enum | `scheduler-card.js` (`Weekend:return"weekend";default:return"daily"`) | Custom day-sets are unreachable from the UI no matter what the backend does |
| Look-ahead capped at ~16 days | `timer.py` `if iteration > 15: … return None` | A sparse day-set (school year with a summer gap) silently arms no timer |
| `day_in_weekdays` is synchronous | `timer.py:284` | Cannot query a calendar (async) from inside day evaluation |

Plus the semantic wart that started this: **"In the weekend" means "the workday
sensor is off"**, i.e. non-workday, and only for *today* — future days fall back
to hardcoded Mon–Fri / Sat–Sun (`timer.py:300-307`). Two different meanings for
one word.

## Decisions

- **Hard fork**, seeded from `snadboy/sb-scheduler-component` git history.
  No upstream merges are planned; the `upstream` remote is for rare cherry-picks
  only. Upstream had one code commit in 2026.
- **New domain `sb_scheduler`, new repo `snadboy/sb-scheduler`.** The old fork
  stays installed and running throughout development — different domains
  coexist, so live schedules are never at risk. Re-create the 3–4 schedules at
  the end, then uninstall the old one.
- **GPL-3.0**, inherited and permanent. Attribution to nielsfaber stays in the
  README and LICENSE.
- **Day-sets are defined in the config-entry options flow** (all visual, no YAML).

### What we inherit vs replace

Measured against the fork's 3,237 lines:

| Component | Fate |
|---|---|
| `actions.py` (707) — queue, retry-when-unavailable, conditions, dispatch | keep whole |
| `websockets.py` (272) — WS/REST API + subscription push | keep, rename domain |
| `store.py` (376) — versioned storage + migration | keep, new schema |
| `switch.py` (545), `__init__.py` (507) | keep, extend |
| `timer.py` — `day_in_weekdays`, `calculate_timestamp`, `next_timeslot` (~250) | **replace** |

The scheduling core is ours. The execution half — the tedious, bug-prone part —
is inherited and already hardened (see the fork's loud-failure work).

## Schedule model

A schedule picks **one day-set** and holds **one or more steps**. A step is a
time pattern plus the actions to run at it. Two XOR choices per step; the
symmetry is deliberate, because it makes the card two radio groups.

```yaml
schedule:
  day_set: workday            # exactly one — see below
  steps:
    - name: On                # what this step does, in the user's words
      enabled: true
      pattern:                # exactly one of:
        occurrences: ["06:30", "18:00"]   # one or more times of day
        # — or —
        interval:                         # fire at start, then every N until stop
          start: "09:00"
          stop:  "13:00"
          every_minutes: 15
      actions: [...]          # unchanged from upstream
      conditions: [...]       # unchanged from upstream
    - name: Off
      pattern: { occurrences: ["sunrise+00:15:00"] }
      actions: [...]
```

### Why steps: the schedule leads with the ACTION, not the time

The first cut had one pattern and one action list per schedule, which forced
"turn the garden lights on at sunset and off at sunrise" into **two schedules**
that a reader has to mentally re-join, and left irrigation unable to express its
own off at all. Leading with the action inverts it: *what happens*, then *when*.
One schedule, two steps, one day-set, one name.

Each step arms independently — `_rearm` computes every enabled step's next
trigger and arms for the earliest, recording which steps share that moment so
several can fire together. `last_triggered` is per step as well as rolled up,
because "did the off step run?" is a different question from "did the schedule
run?".

**Every pattern is still start-only.** "Every 15 min from 09:00 until 13:00"
fires 17 times and emits no stop action; occurrences are points in time, not
ranges. A stop is a second *step*, which is exactly what steps are for — never a
duration hanging off the start.

Pre-steps schedules migrate on load into one implicit step built from their
top-level `pattern`/`actions`, so nothing on disk needs rewriting and no history
is lost.

### An edit must not erase what it did not mention

`async_update` merges an incoming step onto the stored one **by `step_id`**
(`merge_steps`). The card edits a step's name and pattern and knows nothing
about `actions`; a plain list replacement would silently empty them, and — like
the `service_data` landmine — the damage would only surface at FIRE time. The
resulting list is still exactly what was passed, so omitting a step deletes it
and an unknown id inserts one; only the content is merged.

`specific_days` is one of the day-set options and reveals the seven weekday
checkboxes, so nothing the old model could express is lost.

An `interval` pattern expands to concrete times at timer-arming time, not in
storage — so the stored config stays "every 15 minutes from 09:00 to 13:00"
rather than seventeen timestamps. Capped at `MAX_INTERVAL_OCCURRENCES` (288) per
day so `every_minutes: 1` cannot silently produce a firing every minute.

## Day-sets

A day-set answers questions about **dates**, not about *now*. This is the whole
point: a `binary_sensor` can only report the present, which is why upstream has
a today/future split.

```yaml
day_set:
  id: school_day
  name: School Day
  weekdays: [mon, tue, wed, thu, fri]   # base mask, optional
  include: []                           # sources that force eligibility
  exclude:                              # sources that veto
    - dates: ["2026-11-26..2026-11-28", "2026-12-22..2027-01-05",
              "2027-06-05..2027-08-17"]     # breaks + summer, inline
    - dates: ["2026-10-09", "2027-02-12"]   # institute days
  invert: false
```

### A source is a calendar entity *or* an inline date list

```yaml
- entity: calendar.trash_day     # a calendar entity
- dates: ["2026-12-25", "2027-06-05..2027-08-17"]   # explicit dates and ranges
```

This is the answer to "can't we just configure weekdays plus a list of
no-school days" — **yes, and that removes the need for a school integration
entirely.** No `calendar.il_cusd_304` to hand-populate: the dates live in the
day-set options flow, and the day-set still surfaces as `calendar.school_day`
for inspection. Ranges make summer one entry rather than sixty.

**Do not rebuild Trash Day.** `snadboy/ha-sb-trash-day` already computes
`calendar.trash_day` from `pickup_weekday` + `holiday_calendar` + `holidays` +
`shift_days` (including the holiday-shift rule, which a plain mask can't
express). Reference it: `day_set: trash_day → include: [calendar.trash_day]`.
Day-sets generalise that pattern; they don't need to absorb it.

Annual upkeep is unavoidable either way — with no district ICS, next year's
breaks get typed in once a year, whether into a calendar or into this config.

**Precedence: `include` > `exclude` > `weekdays` mask.** This is not invented —
it is exactly the logic of the existing `binary_sensor.workday_sensor` template
helper, where a `days_off` event titled "Workday" reinstates a workday.

### Interface

```python
class DaySet:
    def is_eligible(self, d: date) -> bool: ...
    def next_date_on_or_after(self, d: date) -> date | None: ...
```

`next_date_on_or_after` is the load-bearing method — it is what removes the
16-day ceiling. Cost differs by shape, and honestly:

- **Inclusion-based** (Trash Day): one `calendar.get_events` forward query
  answers it directly. O(1) regardless of gap size.
- **Exclusion-based** (School Day, Workday): walk candidate days forward against
  a **prefetched window** held in memory. A ten-week summer gap is ~70 dict
  lookups, not 70 async calls. Unbounded, but not O(1).

### Prefetch / cache

`day_in_weekdays` being synchronous is why upstream can't do this. We prefetch:

- window: **400 days forward** (`HORIZON_DAYS`), recomputed whole rather than
  extended on demand — simpler, and one calendar query per source covers it.
  Running past it logs an ERROR and returns `None`; it never returns a wrong
  date, and never a silent `None`, which is the upstream failure mode.
- refresh: daily, plus on `calendar` entity state change for any source calendar.
- **multi-day events cover `[start, end)`**, not just their start date. Summer
  break is one event; this is what makes the school year need no season concept.

### Built-ins

`daily`, `specific_days`, `weekend` (literally Sat+Sun), `workday`,
`non_workday`. Note that with named day-sets, **"Weekend" can finally mean
weekend** — "Non-workday" is its own entry, so the original semantic complaint
disappears by construction.

`workday` defaults to `calendar.workday_sensor_us_calendar`. The Workday
integration publishes a calendar entity that projects its full config
(country, excludes, `add_holidays`, `remove_holidays`) onto arbitrary future
dates — verified 2026-09-19: the configured entry lists 2026-11-11 (Veterans
Day, in `remove_holidays`) as a workday while `calendar.workday_reference`
skips it. This is the date-queryable source upstream never consumed.

### Published as calendars

Every day-set is also exposed as `calendar.<id>`. Costs little, and makes a
day-set debuggable by looking at it in HA — which matters the first time School
Day disagrees with you.

## Card

A new card is unavoidable (the old one's vocabulary is a fixed enum) and is the
largest single piece of work. Scope for v1:

- day-set radio group + `specific_days` checkbox reveal
- pattern radio group: occurrence list editor / interval editor
- entity + action picker, conditions, start/end dates — ported in behaviour from
  nielsfaber's card, which remains the reference for what "good" looks like.

During Phase 1 there is **no editor** for the new domain; schedules get created
via service calls / developer tools. Acceptable for 3–4 schedules over a short
window, and the old integration keeps running the real ones meanwhile.

## Phases

| | Deliverable | Independently useful? |
|---|---|---|
| **0** | ~~Day-set library: config-entry options UI, calendar backing, `next_date_on_or_after`, prefetch cache, calendar entities~~ **DONE** | Yes — usable by automations and dashboards with no scheduler at all |
| **1** | `sb_scheduler` integration: new schema, new timing core, execution inherited | Yes — schedules run; created via services |
| **2** | `sb-scheduler-card` | The new shapes become reachable |
| **3** | Re-create the 3–4 live schedules, uninstall `sb-scheduler-component` | Done |

## Resolved

- **School calendar** — no separate integration, no calendar entity to populate.
  `school_day` is a day-set: Mon–Fri minus inline break ranges and institute
  days, typed into the options flow (2026-09-19).
- **Interval semantics** — start-only repeats; ranges stay in `occurrences`
  (2026-09-19).

## Sun-relative occurrences

An occurrence is `HH:MM`, or `sunrise`/`sunset` with an optional offset
(`sunset+00:15:00`, `sunrise-01:30`). Required by a real schedule: garden
lights run sunset+15 to sunrise+15.

Resolved **per date** via `get_astral_event_date`, not from `sun.next_rising`.
Upstream reads the sun entity's next_rising/next_setting attributes, which only
describe the *next* event — the same now-only limitation as the workday sensor,
and unusable for a trigger three weeks out.

`timer.py` stays HA-free: the resolver arrives as a `sun_resolver(event, date)`
callback, so the whole module still tests offline.

**Gotcha:** fixed times resolve naive, HA sun times resolve aware, and sorting a
mixed list raises TypeError. A schedule with only a sun time survives (a
one-element sort never compares) — one mixing `00:00` with `sunset+00:15` does
not. `times_on` normalises before sorting. This shipped broken and was caught
only by recreating a real schedule.

## RESOLVED: three-tier day-sets (was: the workday day-set ignores days_off)

The cutover dropped PTO handling. The old chain was Workday integration +
`calendar.days_off` → template sensor; the new `workday` day-set reads only
`calendar.workday_sensor_us_calendar`. Verified: 2026-11-27 and 2026-12-24 are
in `days_off` but report **eligible=True**, so the wake-up light will fire on
both.

Fixed 2026-09-20 by splitting the tiers. Precedence is now **force > veto >
base**:

1. **force** (`force_calendars` / `force_dates` / `force_match`) — wins outright
2. **veto** (`exclude_*`, with `exclude_match`) — cancels an eligible day
3. **base** (`weekdays` mask and/or `base_calendars` / `base_dates`)

`include_*` used to mean the base tier and is still read as such, so existing
config keeps working with no migration step.

`*_match` is a case-insensitive substring test against an event's **summary and
description**. Both are needed: a days-off entry is identified by its summary
("Workday"), while Google's holiday feed marks the real ones only in the
description ("Public holiday" vs "Observance") — which also closes that open
question.

Live wiring: `workday` = base `calendar.workday_sensor_us_calendar`, veto
`calendar.days_off`, force `calendar.days_off` matching "Workday".

Verified live 2026-09-20, all three tiers:

| | |
|---|---|
| veto | 2026-11-27 and 2026-12-24 (days-off entries) report **False** |
| force | a "Workday"-titled entry added to `days_off` on Sat 2027-01-09 flipped `workday` to **True** — and that same entry is in the veto source, so it was vetoed and force-overridden at once |
| base | Thanksgiving still False, Veterans Day (in `remove_holidays`) still True |

The test entry was deleted afterwards; `calendar.days_off` is back to its
original two entries. There is no `calendar.delete_event` service — deletion is
the websocket command `calendar/event/delete` with `entity_id` and `uid`.

## Known wart: the work week is written twice

"Which days are work days" lives in two places — the **Workday integration's**
own `workdays` mask (which drives `calendar.workday_sensor_us_calendar`, and
therefore the `workday` / `non_workday` day-sets), and the **`weekend` day-set's**
mask. Change jobs to a Wed–Sun week and both need editing; miss one and Weekend
and Non-workday disagree silently. We cannot fix it by fiat because the Workday
integration owns its mask.

Verified 2026-09-19 that the mask *is* freely editable (Weekend set to Mon/Tue,
matched Mon+Tue, reverted) — so this is a consistency risk, not a capability gap.

The fix, when it's worth doing: **day-set composition** — let a day-set
include/exclude *another day-set*. Then `work_week` is defined once as a pure
mask and `weekend = invert(work_week)`. It would also let School Day exclude
Non-workday instead of re-listing holidays. Deferred deliberately: building it
now would solve a problem nobody has yet.

*Update 2026-09-21:* half of this arrives as `exclude_day_sets` in the
self-sufficient day-sets design below — the Workday integration's mask goes
away with the integration, so the duplication shrinks to sb_scheduler's own
masks, which one config owns.

## Naming: day-set calendars can collide with their sources

A day-set named after the calendar it is built from collides: "Trash Day"
backed by `calendar.trash_day` was assigned `calendar.trash_day_2` (renamed by
hand to `calendar.sb_trash_day`). The `sb_` prefix this design originally
proposed and then dropped would have avoided it. Five existing day-set
calendars are bare; if the collision recurs, prefix them all.

## Offsets: `offset_days`

`offset_days` shifts every date in the resolved set. "The night before trash
day" is the Trash Day config with `offset_days: -1`.

It lives on the **day-set**, not on a schedule's reference to one: a shifted
set of dates is just another set of dates, so it composes with everything and
surfaces as its own `calendar.*` entity that automations and dashboards can use
too.

Applied **last** — after force/veto/base and after `invert` — so it always
means "that set, moved", which is the only reading that stays predictable when
combined with invert.

Evaluation widens the window by `|offset|` on both sides before shifting and
clips back afterwards; without that, a `-1` set loses its first date at the
window edge.

Day-sets also compute **`PAST_DAYS` (45) into the past**, because they are
published as calendar entities and HA's calendar panel opens on the current
month — it routinely asks about dates before today. `is_eligible` warns only
past the forward horizon (a real limit); a question about a date before the
window is ordinary and logs at DEBUG.

The holiday shift compounds correctly and for free: when Trash Day moves from
Fri 2026-11-27 to Sat 11-28 for Thanksgiving, Trash Day Eve moves from Thu to
Fri with it.

## Derived day-sets: base, pick, months, optional calendar (2026-09-21)

The question that forced this: *"every other Tuesday, every third day, first
weekday of the month, day after the first Monday of November"*. None of those
are expressible by a mask plus calendars. They are **transformations of a set
of dates**, and once there are several that must compose (Election Day uses
three), they belong in day-sets — the thing that already answers "which
dates" — not on the schedule, where they would be duplicated per schedule and
could never be named or reused.

A day-set therefore gained, in evaluation order:

| Stage | Knob | Notes |
|---|---|---|
| base | `base_day_set` | build on another day-set's resolved dates, alongside mask/calendars/dates |
| tiers | force > veto > base | unchanged |
| **pick** | `pick` = `every` (N, anchor) or `nth_of_month` (1–5, last) | strides over **eligible** dates, not calendar days |
| **months** | `months` | keep only these months |
| invert | unchanged | after pick, so "NOT the first Monday" means what it says |
| offset | unchanged, still last | so "day before the first Monday of January" may land in December |

`expose_calendar` (default on) is the answer to "no calendar device per
special case": a derived set simply opts out. The card discovers day-sets from
a single **roster sensor** now, not from calendar entities.

Election Day = base Monday → pick 1st of month → months [11] → offset +1.
Resolves to 2026-11-03, 2027-11-02, 2028-11-07; offline test.

**Why stride over eligible dates.** "Every N days" cannot express "every other
workday" (unevenly spaced), and "every other trash day" must keep its phase
when Thanksgiving pushes a collection from Friday to Saturday — the count is
unchanged, only the date moved. The cost, stated honestly: inserting or
removing a date in a calendar-backed base re-phases everything after it. A
holiday *shift* does not; an *insertion* does. Mask-only bases can never
re-phase.

**Anchor aging.** Day-sets compute 45 days into the past. An `every` anchor
older than that cannot be counted from, so the registry pulls the window start
back to the oldest anchor — capped at `MAX_ANCHOR_AGE_DAYS` (3 years), past
which it warns and counts from the cap.

**Nth-of-month only picks from months fully inside the window.** A month cut
by the window edge cannot say which date was truly first or last, and a wrong
"first Monday" is worse than none. With PAST_DAYS = 45 the current month is
always whole.

**Dependency order.** `base_day_set` chains are sorted topologically at
registry construction (`order_by_dependency`, pure, tested). A missing base
or a cycle is an ERROR in the log and the set evaluates with an empty base —
never a hang, never a silent nothing. The options flow refuses to save a
cycle in the first place.

**The form.** Twenty flat fields was a wall, so the options flow is five
collapsible `section()`s — Sources, Cancelled, Always, Pick, Advanced — each
opening expanded only when it holds a value. Storage stays flat; `flatten()`
folds a submission back.

**Day-set editing from the card (same day, v0.3.1 + card v0.10.0).** Two
services, `set_day_set` and `remove_day_set`, write the config entry's
options (which reloads the entry) and validate with `validate_day_set` — the
identical function the form calls, moved into `day_set.py` so the two entry
points cannot drift. The roster sensor carries each day-set's stored config
and its dependents (`used_by`), so the card needs no websocket API here
either. Removal is refused while a schedule or another day-set depends on
the set: a silently broken schedule is the exact failure this integration
exists to prevent. The Configure form stays as the second door.

## Schedule negation (2026-09-21)

"Is the only reason we have both Workday and Non-workday that a schedule has
no negate?" — yes. A schedule now carries `negate: bool`; when set, the
timer is handed `NegatedDaySet(base)`, the complement, and Non-workday was
deleted from the live config (nothing referenced it).

This is a deliberate exception to the rule above that transformations live
in day-sets. I recommended keeping it there for consistency; the user chose
fewer named inverses, and negation is small enough that two homes (day-set
`invert`, schedule `negate`) cost almost nothing. The card shows a negated
schedule with a hollow "not workday" chip so it cannot be misread as a
day-set called that. If Daily is negated the schedule can never fire; the
wrapper logs an ERROR rather than a silent None, same as an empty horizon.

## Self-sufficient day-sets: holidays, subtraction, tags (designed 2026-09-21, not yet built)

### The problem

"Why is the day after Thanksgiving a day off but not Thanksgiving?" — because
`calendar.days_off` was never a list of days off. It was a **Local Calendar a
Claude session created on 2026-09-16** as the place to type corrections to
the Workday integration's holiday list, seeded with two example entries that
nobody questioned, and shown in HA's calendar panel under a name that
promises the whole picture. The user's verdict: a hand-maintained local
calendar is a non-starter.

Behind it, the Workday day-set is assembled from three external scaffolds:

| Scaffold | Owner | What it contributed | Consumers found |
|---|---|---|---|
| `calendar.workday_sensor_us_calendar` | Workday integration ("Workday Sensor US") | Mon–Fri minus US federal holidays, minus Columbus / Veterans / Washington's Birthday / Juneteenth | this day-set only |
| "Workday Reference (all holidays)" | a second Workday entry | comparison only | none |
| `calendar.days_off` | Local Calendar | PTO; "Workday"-titled entries reinstate | this day-set + `binary_sensor.workday_sensor` |
| `binary_sensor.workday_sensor` | template helper | combined the above for the old scheduler | **none** (all dashboards, automations, scripts and helpers searched) |

Every boot race fixed so far (§CLAUDE.md) came from reading those external
calendars during setup. Only one external source has a reason to exist: the
user's own Google calendar (Anderson), which is where they already put their
life.

### Not a second integration

The user asked whether an "SB Calendar" integration publishing Workday / Days
Off / Holidays / Weekend as true calendars would have been the better shape.
**It is — and it is what day-sets already are**: each one is published as
`calendar.<id>`. Splitting the calendar layer out of sb_scheduler would
reintroduce a cross-integration read (the boot-race class) and a second
refresh loop. So the design stays one integration and makes the day-set layer
**self-sufficient**: two new capabilities, then Workday, Holiday and Day Off
become ordinary day-sets.

### New capability 1: a native holidays source

A day-set's Base tier gains a **holidays** source, computed in-process with
the `holidays` library — the same library, same version (0.104), the Workday
integration uses, already present in the core image; the manifest declares
`holidays>=0.104` (a floor, not a pin, so a core bump can never conflict).

```yaml
holidays_country: US          # required to enable the source
holidays_subdiv:  ""          # optional state/province
holidays_observed: true       # Sat/Sun holidays shift to Fri/Mon, as federal rules do
holidays_remove:  [Columbus Day, Veterans Day, Washington's Birthday, Juneteenth]
```

`holidays_remove` is matched with `pop_named`, which also drops the
"(observed)" twin — the Workday integration's own approach. Standing extras
("Day after Thanksgiving", if wanted every year) are not a holidays knob:
they are `base_dates` on the same set, or `#do` on Anderson for one year.

The card cannot import the library, so a **service with a response**,
`sb_scheduler.list_holidays {country, subdiv, year}`, returns the names for
that country; the editor renders them as chips (ticked = a day off). "Still
work Columbus Day" becomes un-ticking a chip, not typing a string that has to
match.

### New capability 2: subtract another day-set

`base_day_set` already lets a set build **on** another (a base-tier source).
Its mirror, `exclude_day_sets: [ids]`, makes other day-sets a **veto-tier**
source: their dates are removed. Both kinds of edge feed
`order_by_dependency`, so a cycle is refused at save time exactly as today.
(`force_day_sets` would be the third mirror and is free once the edges exist;
not built until something needs it.)

This is the "day-set composition" the *Known wart* section deferred, arriving
because something finally needs it.

### The resulting graph

| Day-set | Definition | Calendar |
|---|---|---|
| `holiday` | holidays source (US, remove the four, observed on) — all holidays, weekends included | `calendar.holiday` — new |
| `workday` | mask Mon–Fri; **exclude_day_sets [holiday]**; exclude_calendars [anderson] match `#do`; force_calendars [anderson] match `#wd` | `calendar.workday` — same id, so the four live schedules are untouched |
| `day_off` | mask Mon–Fri; exclude_day_sets [workday] | `calendar.day_off` — new; every non-working **weekday**: holidays, PTO, `#do` |
| `weekend` | mask Sat–Sun (unchanged) | `calendar.weekend` |

Decisions taken with the user (2026-09-21): Day Off is weekdays only, Weekend
stays its own set; tags are `#do` (off) and `#wd` (work anyway); tags are read
from the event **title only**; a **timed** `#do` event (half-day PTO) counts
as a full day off; **no** `binary_sensor` per day-set (nothing consumed the
old one); a **refresh button** is wanted.

Assumed pending confirmation: the holiday list stays exactly today's rule
(federal minus those four, observed shifts on). The two seeded Days Off
entries (2026-11-27, 2026-12-24) are *not* carried over automatically — if
they are real PTO the user re-adds them as `#do` on Anderson or as
`exclude_dates` on Workday.

### Matching becomes whole-token, title-only

`*_match` today is a case-insensitive **substring** of summary *or*
description, so `#do` would fire on "#done" and a note mentioning "#do" in
passing would cancel a day. It becomes a case-insensitive **whole-token**
match against the **title only**: the title is split on whitespace, trailing
punctuation stripped, and a rule matches when one token equals it. The
per-calendar rule syntax (`calendar.x: #do`) is unchanged. This is a
deliberate breaking change — nothing live depends on description matching or
on substrings (the "Workday" reinstate rule goes away with Days Off) — and it
retires the open question about filtering Google's holiday feed by
description: that feed is not a source any more.

### Timed events cover every local date they touch

An all-day event stays half-open (`[start, end)`). A timed event covers each
local date from its start through its end, inclusive — except an end falling
exactly on midnight, which is exclusive, so 22:00–00:00 is one day, not two.
This is what makes a two-hour `#do` block a day off, per the decision above.

### Refresh button

`button.sb_scheduler_refresh` (button platform — a button is not a sensor) so
the refresh can sit on any dashboard, plus a ↻ in the card header calling the
same `sb_scheduler.refresh`. Both are for the impatient case: an event added
to Anderson is otherwise seen within Google's ~15-minute poll plus our
15-minute interval refresh (§CLAUDE.md).

### Export

The config entry is a `.storage` JSON file HA owns — backed up with HA,
editable through the UI, but not something to diff or version. Cheap fix:
config-entry **diagnostics** (`async_get_config_entry_diagnostics`) return
the day-sets and schedules as JSON, downloadable from the integration page.
No new service.

### Migration and acceptance

Build order: engine (holidays source, `exclude_day_sets`, token matching,
timed-event dates) with offline tests → `list_holidays` service + button
platform + diagnostics → card (holiday chips, minus-day-set chips, ↻) → live
config: create `holiday`, rewrite `workday` in place, create `day_off` →
delete the four scaffolds (Days Off local calendar, the template helper, both
Workday entries).

**Acceptance is a diff, not a feeling:** capture `calendar.workday`'s 400-day
date list before the rewrite and after; the only differences allowed are the
two seeded Days Off dates (which stop being off) and nothing else. Same for
`calendar.day_off` against the complement of the old Workday calendar on
weekdays. Then one real `#do` on Anderson, watched through to
`calendar.day_off` within 30 minutes.

**What this does not fix:** the work week is still written twice (Workday's
and Day Off's Mon–Fri masks, Weekend's Sat–Sun). One named `work_week` mask
that the others build on would end that; deferred again, because the user has
one job and one week.

## Open questions

- Whether day-sets should be shareable across config entries or scoped to one.
- ~~Whether `exclude` needs a title/description filter for calendar sources.~~
  Resolved: per-calendar match rules (v0.4.1), and the Google holiday feed is
  no longer a candidate source once holidays are computed natively (§above).
