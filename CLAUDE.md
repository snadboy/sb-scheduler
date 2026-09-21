# sb-scheduler — session notes

**Read [DESIGN.md](DESIGN.md) first** — it holds the model, the phases and the
reasoning. This file is working notes.

Hard fork of `nielsfaber/scheduler-component`, diverged at v3.3.8 by way of
`snadboy/sb-scheduler-component`. 341 commits of history inherited so the GPL-3
attribution chain stays intact. **No upstream merge path is planned.**

| | |
|---|---|
| Repo | github.com/snadboy/sb-scheduler — local `~/projects/git/sb-scheduler` |
| Domain | `sb_scheduler` (NOT `scheduler` — see below) |
| License | GPL-3.0, inherited and permanent |
| `fork` remote | github.com/snadboy/sb-scheduler-component — the frozen predecessor |
| Status | **Phases 0, 1 and 2 (edit-only card) done and verified live. Steps model landed 2026-09-20.** |

## Steps (2026-09-20)

A schedule holds **one or more steps**, each a pattern + its own actions — see
DESIGN.md. Garden Lights is now ONE schedule with an On and an Off step; the
five schedules became four.

- **`async_update` merges steps by `step_id`** (`merge_steps`). The card sends
  only `{step_id, name, enabled, pattern}`; without the merge, `actions` would
  be silently emptied and it would only show when the schedule fired.
- **A step toggle must send every step_id**, because the resulting list is
  exactly what was passed — omitting a step deletes it. The card's
  `_toggleStep` maps over all of them for this reason.
- `run_now` takes an optional `step_id`; without one it runs every step.
- `next_trigger(pattern, day_set, now, sun_resolver, label)` takes a PATTERN
  now, not a schedule, with `label` only for logging.
- **Step ids are allocated, not positional** (`allocate_step_ids`). `s{index+1}`
  collides: delete the first of two steps, add a new one, and both land on
  `s2` -- after which `merge_steps` overlays two steps onto one stored step and
  `_handlers` / `_step_next`, keyed by step id, collapse the pair so one
  silently never fires. An id an earlier step already claimed is reallocated
  for the same reason. The card relies on this: it saves new steps with NO id.

## Derived day-sets (2026-09-21) — see DESIGN.md for the model

`base_day_set` + `pick` (`every` N from anchor / `nth_of_month` 1–5, last) +
`months` + `expose_calendar`. Evaluation: base → tiers → pick → months →
invert → offset. Acceptance case is Election Day (Mon → 1st of month → Nov →
+1), in `tests/test_day_set.py`.

- **A day-set without a calendar is invisible to the old card.** The card
  discovered day-sets by scanning `calendar.*` for `day_set_id`. That is why
  `sensor.py` exists: one roster sensor (`roster: sb_scheduler`, `day_sets:
  [...]`) that the card (≥ 0.9.5) reads first. Keep the roster attribute shape
  stable — the card depends on it.
- **`_purge_orphaned_entities` must treat a calendar-off day-set as invalid**
  or its old calendar entity lingers `restored`. And the roster sensor's
  unique_id must be in the valid set, or the purge deletes it on restart —
  the same trap as the schedule switches.
- **The options form uses `section()`; submissions arrive NESTED.** Every
  step must `flatten()` before validating or saving. Errors go under `"base"`
  (form-level); a section named `base` would collide with that, which is why
  the first section is called `sources`.
- **`DateSelector` rejects `""` as a default.** The anchor field only gets a
  default when a real date exists.
- **`SelectSelector` needs an explicit none option** for the base picker —
  `NO_BASE = "__none__"`, mapped back to `""` in `flatten()`.
- Months are stored as the strings the form emits (`"11"`); `from_config`
  accepts ints too.

**Boot race (found 2026-09-21, probably always there).** sb_scheduler can set
up ~10 s into boot, before `trash_day` / `workday` have created their
calendars. `calendar.get_events` then raises *"Service call requested response
data but did not match any entities"*, every calendar-backed day-set computed
EMPTY, and the log filled with tracebacks plus "no eligible date … nothing will
be scheduled". It always self-healed — the registry's source-change listener
fires when the entity appears and recomputes — but it looked like an outage
and briefly armed schedules against nothing.

It has TWO shapes, and the first fix only caught one. (1) The entity does not
exist: `collect()` skips it. (2) **The entity has a state but its platform
cannot serve it yet** — the state-change listener fires on that first state
write, we query, and `get_events` STILL raises "did not match any entities".
So that specific failure is treated as not-ready too (`_calendar_dates`
returns `None`), both land in `_missing_sources`, `next_date_on_or_after`
logs a WARNING "waiting for …" rather than the ERROR, and the registry arms a
one-shot `async_call_later` retry (`MISSING_SOURCE_RETRY_SECONDS`) so
convergence never depends on the source emitting another state change. Any
OTHER exception from a calendar is still logged as an error, not swallowed.
A plain entry reload never hits this; only boot does. Tests: `LateHass`,
`HalfUpHass`, `BrokenHass` in `tests/test_day_set.py`.

## Where things are

- `custom_components/sb_scheduler/` — the live integration: day-sets (Phase 0)
  plus schedules (Phase 1).
- `reference/scheduler-component/` — the inherited tree, **deliberately outside
  `custom_components/`** so HACS cannot install a second `scheduler` domain
  alongside the running one. `actions.py` is already ported from here;
  `websockets.py` is still unported (only needed if the card ever outgrows
  reading entity attributes).
- The card lives in **its own repo**: `snadboy/sb-scheduler-card`
  (`~/projects/git/sb-scheduler-card`), HACS custom repo id **1378324666**,
  category Dashboard, MIT. Split out 2026-09-20; it used to be `card/` here.
- `tests/test_day_set.py` (31 checks) and `tests/test_timer.py` (19) — run with
  plain `python3`, no HA needed. `day_set.py`'s few HA imports are stubbed;
  `timer.py` has none at all, so the replaced scheduling core tests natively.

## Deploying to HA (no HACS yet)

`scp` fails — the HAOS SSH add-on has no sftp subsystem. Pipe through stdin:

```bash
tar czf /tmp/sb.tgz -C custom_components sb_scheduler
ssh snadboy@homeassistant "cat > /tmp/sb.tgz" < /tmp/sb.tgz
ssh snadboy@homeassistant "cd /config/custom_components && tar xzf /tmp/sb.tgz && rm /tmp/sb.tgz"
```

`rm -rf` on the deployed dir fails: `__pycache__` is root-owned (HA runs as root
in the container) while the SSH user is `snadboy`. Extract over the top instead —
stale `.pyc` files are invalidated by source mtime.

**Reloading the config entry does NOT re-import changed Python.** A code change
needs a full restart, or you will verify the old behaviour and believe it. This
cost a cycle on the slugify fix.

**SSH as `snadboy@homeassistant`; `sudo` works, plain `docker` does not.**
`/config/custom_components` is writable directly, but `/config/www` is root-owned
so the card needs `sudo tee`. `docker exec` without sudo fails with a socket
permission error.

**DANGER: that failure is silent if you discard stderr.** Several "logs are
clean" checks in this project were `docker exec ... 2>/dev/null | grep`, which
returned nothing because the command never ran — not because the log was clean.
Read logs with `sudo docker exec …` and check the byte count, or don't trust the
result. (`/api/error_log` is 404 on 2026.9.)

## Phase 0 notes

- Day-set ids are **slugs of the name** (`school_day`), not random tokens.
  Phase 1 schedules reference day-sets by id, so an opaque id would end up in
  every schedule's config and every service call.
- Removing a day-set leaves an orphaned entity registry entry, so the next
  day-set with that name lands on `_2`. `_purge_orphaned_entities()` on setup
  fixes it — HA only cleans the registry when the whole entry is removed.
- The 400-day horizon is the point, not a magic number: a school year has a
  ~10 week summer gap and upstream's 16-iteration cap is the bug being replaced.
  `next_date_on_or_after` logs an ERROR rather than returning a silent `None`.

## The coexistence rule

**`sb-scheduler-component` stays installed and running on HA throughout
development.** It owns the live schedules (bedside lamps, garden irrigation,
garden lights). Different domains coexist, so nothing here can break them.
Do not uninstall it until Phase 3 re-creates those schedules here.

This is why the domain changed: with only 3–4 schedules, migration was never
worth a design compromise, and the old integration is a better safety net than
a compatibility shim would have been.

## Why the old card is not a fallback

An earlier plan kept the `scheduler` domain so nielsfaber's card could edit
schedules while ours was half-built. That was wrong: the card's day vocabulary
is a hardcoded enum (`Weekend:return"weekend";default:return"daily"`), so it
cannot express any schedule that uses a day-set. It would only ever have edited
the schedules we don't care about. During Phase 1 there is simply **no editor** —
create schedules via service calls / developer tools.

## Gotchas carried over (still true)

- **Execution lives in `ActionQueue`, not `ActionHandler`.** `async_process_queue`
  is on ActionQueue. Check which class owns a method before adding helpers next
  to it — a first cut here raised AttributeError only under live test.
- **`gh release create` inside a fork targets the PARENT repo.** Always pass
  `--repo snadboy/sb-scheduler`.
- **HA core logs are not in `docker logs homeassistant`**, `/config/home-assistant.log`
  does not exist, and `ha core logs` over SSH returns 401. What works:
  `docker exec homeassistant sh -c 'TOKEN=$(cat /run/s6/container_environment/SUPERVISOR_TOKEN); curl -s -H "Authorization: Bearer $TOKEN" http://supervisor/core/logs?lines=20000'`
  (MQTT debug floods it — take a big window and grep).
- **Persistent notifications are not entities.** Use the WS command
  `persistent_notification/get`; dismiss via the `persistent_notification.dismiss`
  service (there is no WS `dismiss`).
- **Silent no-op replaces.** `python`/`sed` string replacement has silently
  matched nothing twice in this project. Grep to confirm before saving.

## Inherited behaviour worth keeping

The two fixes from the predecessor fork carry forward and must not regress:

1. `ActionQueue.is_available()` failures are **loud** — ERROR log + an event +
   optional persistent notification, once per occurrence, with retry preserved.
   Upstream logged at DEBUG and returned, so a schedule looked like it ran.
2. The workday source is **configurable**, not a hardcoded entity id. In the new
   model this generalises into day-sets and should read a *calendar*, not a
   binary_sensor — see DESIGN.md on why the sensor can only answer "now".

## Day-set tiers (and the days_off fix)

Precedence is **force > veto > base**; see DESIGN.md. `include_*` is the legacy
name for the base tier and is still read.

**Renaming a DaySet field breaks the calendar platform silently.** Changing
`include_calendars` to `base_calendars` left `calendar.py` reading the old name,
so every day-set calendar came back `unavailable` with `restored: true` and NO
error in the log. The schedules kept firing, because they use the registry
objects rather than the calendar entities — so the breakage was invisible from
the thing that matters. Grep every module for a field before renaming it.

**Editing a local calendar from a script:** `calendar.create_event` is a
service, but there is **no `calendar.delete_event`** — deletion is the
websocket command `calendar/event/delete` with `entity_id` + `uid`. Probe it
with a bogus uid before creating anything, so you know you can clean up.
(`UID` is a readonly bash variable; name the shell var something else.)

`snadboy/ha-workdays-card` was **removed 2026-09-20**. It lived inside the
`#workdays` Bubble pop-up on the Home view; that pop-up now holds a native
`calendar` card for `calendar.days_off` + the workday calendar, plus a note
that entries are added in HA's Calendar panel. The "Workdays" button and the
pop-up itself are unchanged. Backup:
`/config/backups_workdayscard_20260920_071541/`.

**`.storage` files lag the live state.** Right after removing the repo, the
on-disk `lovelace_resources` still listed the card — HA writes storage lazily.
The live API already showed it gone. Check the API before concluding a cleanup
failed.

## Recreating the real schedules

The three live schedules map onto four sb_scheduler schedules, all created
**disabled** — the old integration still owns them, and double-firing irrigation
is a real-world consequence, not a cosmetic one.

| Original | Becomes |
|---|---|
| Bedside Lamps - Wakeup (`workday`, 06:30) | one schedule, unchanged |
| Garden Irrigation (`daily`, `06:00 - 07:00`) | one schedule at 06:00. **The 07:00 stop fires nothing upstream** — actions map 1:1 to timeslots and there is only one action, so the window end is cosmetic. b-hyve ends its own cycle. |
| Garden Lights (3 slots, 3 different actions, sun-relative) | **two** schedules: On at `00:00` + `sunset+00:15:00`, Off at `sunrise+00:15:00` |

Garden Lights exposed the one real modelling gap: upstream allows **a different
action per timeslot**, this model has one action list per schedule. Splitting by
action is a faithful and arguably clearer translation, but a schedule with many
distinct per-time actions would need per-occurrence actions.

Cutover: enable the sb_scheduler one and disable the old integration's, one at
a time. Do not run both.

**Cut over 2026-09-19: ALL of them.** Every schedule on the old integration is
disabled; `sb_scheduler` now owns bedside lamps, garden irrigation and the two
garden-lights schedules. The old integration is still installed (uninstalling is
the last step, once this has run unattended for a while).

**`switch.b_hyve_node_garden`** (friendly name "B-Hyve Outlet" since
2026-09-19) **is a TP-Link HS103 smart plug** (`platform: tplink`), not the
b-hyve irrigation integration — that device is "Irrigation Raised Garden"
(`valve.raised_garden_raised_gardens_zone` and friends). It powers the b-hyve node; the
watering schedule itself lives in the b-hyve app. So "Garden Irrigation" is a
daily 06:00 "make sure the controller has power" re-assert, and firing it is
idempotent — it does not start watering. It has sat `on` for days at a time,
which is the tell.

### The action-shape landmine

`parse_service_call` reads `action["service_data"]` with an **unconditional**
subscript, so an action without that key raises KeyError **when the schedule
fires** — not when it is created. Three of the four recreated schedules were
written without it and would have died on their first real run. `normalise_action`
in the store now supplies `{}` and accepts `data` as an alias (the key
scheduler-component's own storage uses). It runs on load too, so already-stored
schedules are repaired in memory even before the file is rewritten.

## Last run

`last_triggered` is stored **in the schedule**, not held in memory, so a restart
does not erase it — "did last night's run actually happen?" is exactly the
question asked after a restart. `normalise_schedule` carries it through every
edit, so renaming a schedule cannot wipe its history.

It records the **firing**, not the outcome: actions retry asynchronously when a
target is unavailable, so "it ran" and "it succeeded" are different questions.
Failures are reported separately by the action queue's loud-failure path.

## Phase 2 notes (card) — see snadboy/sb-scheduler-card for the current set

- **Edit-only by design.** Creating schedules and editing actions are most of the
  work; `sb_scheduler.create_schedule` already covers creation. This got a usable
  UI in one pass instead of three.
- **No websocket API needed.** The card reads schedules from their switch
  entities' attributes and day-sets from `calendar.*` entities carrying
  `day_set_id`, then writes via `sb_scheduler.edit_schedule`. Phase 2 therefore
  needed exactly one backend change: exposing the RAW `pattern` on the switch,
  because `times` is the expansion and editing that would lose the interval.
- **Never re-render while the editor is open** — `set hass` returns early when
  `this._open` is set. Otherwise a state update mid-typing discards the draft.
  The draft is local state, never read back from hass.
- **A resolved time hides its source.** "6:50" says nothing about tracking
  sunrise, so the backend also exposes `times_detail` ({time, event,
  offset_minutes}) and the card renders `6:50 (after sunrise)`. The magnitude
  is deliberately omitted — the resolved clock time already says when it fires. The card
  cannot derive this by zipping `times` against the stored occurrences, because
  resolution sorts by clock time and reorders them — hence `_resolved()` pairing
  each moment with its occurrence.
- Row controls (v0.4.0): an enable **toggle** (a styled checkbox, not
  `ha-switch`, for the same reason as the plain inputs), **Run now**, and
  **Edit**. `Last:` sits above `Next:`; a disabled row shows `Next: —`.
- **Run now has no confirmation step.** It fires actions immediately, but the
  button is explicit and the consequence is one run of something the schedule
  does anyway. The `Last:` line updating is the receipt — and in practice the
  transient "Running…" label is invisible, because the service returns and
  `last_triggered` re-renders the row faster than the eye.
- `.row` uses `flex-wrap` and `.info` has `flex: 1 1 180px`, so the three
  controls drop to a second line on a phone rather than crushing the text.
- **Occurrences are edited as STRUCTURE, never as text** (v0.7.0). A row is a
  Time↔Sun slider plus either a time picker, or {event dropdown, +/− dropdown,
  minutes spinner}. `parseOccurrence` / `serialiseOccurrence` convert to and
  from the stored string. A misspelling like `sccunrise-00:15` is simply not
  expressible, which removed a whole bug class:
  v0.5.1 picked `type=time` vs `type=text` from `isSun(value)`, so a typo fell
  back to a time picker that refuses to display it — the row went blank, the
  value was unreachable, and `sunset+` could not be typed at all.
  An offset of 0 serialises to a bare `sunset`, and unparseable stored data
  recovers to a `06:30` clock row rather than being uneditable.
- Plain `<input>` elements, not `ha-textfield`: it renders invisible outside
  `ha-form`. Native `<select>` needs explicit `option` colours plus
  `color-scheme: light dark` or its popup ignores the theme.
