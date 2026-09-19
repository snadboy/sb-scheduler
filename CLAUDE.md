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
| Status | Phase 0 not started. The tree is still the old `scheduler` component, unmodified. |

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
