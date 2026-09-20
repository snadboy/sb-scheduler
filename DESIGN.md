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

Two XOR choices. Symmetry is deliberate: it makes the card two radio groups.

```yaml
schedule:
  day_set: workday            # exactly one — see below
  pattern:                    # exactly one of:
    occurrences: ["06:30", "18:00"]     # one or more times of day
    # — or —
    interval:                           # fire at start, then every N until stop
      start: "09:00"
      stop:  "13:00"
      every_minutes: 15
  actions: [...]              # unchanged from upstream
  conditions: [...]           # unchanged from upstream
```

**Both patterns are start-only.** "Every 15 min from 09:00 until 13:00" fires
17 times and emits no stop action; occurrences are points in time, not ranges.
That is the spec as stated — ranged behaviour (irrigation's "on at 06:00, off at
07:00") was my addition inherited from upstream's timeslots, and is NOT built.
If it is wanted, it is a second action list at a stop time, not a duration.

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

## OPEN: the workday day-set ignores calendar.days_off

The cutover dropped PTO handling. The old chain was Workday integration +
`calendar.days_off` → template sensor; the new `workday` day-set reads only
`calendar.workday_sensor_us_calendar`. Verified: 2026-11-27 and 2026-12-24 are
in `days_off` but report **eligible=True**, so the wake-up light will fire on
both.

**The two-tier precedence cannot express the fix.** `include > exclude > mask`,
and the workday calendar is an `include`, so adding `days_off` as an `exclude`
loses. The template sensor's real logic needs three tiers:

1. **force** — a `days_off` entry titled "Workday" reinstates a workday
2. **veto** — any other `days_off` entry cancels it
3. **base** — the workday calendar, or the weekday mask

`include` currently conflates 1 and 3. Splitting them out (a `base_calendars`
tier, leaving `include` as force-only) is the fix, plus a storage migration.
The "Workday"-titled override also needs the calendar title filter already
listed under Open questions.

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

## Open questions

- Whether day-sets should be shareable across config entries or scoped to one.
- Whether `exclude` needs a title/description filter for calendar sources.
  Relevant because `calendar.holidays_in_united_states` is the *Google* holiday
  calendar and mixes public holidays with observances — Black Friday, Election
  Day and "Daylight Saving Time ends" all appear alongside Thanksgiving,
  distinguished only by the `description` field.
