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
| Status | **Phases 0, 1 and 2 (edit-only card) done and verified live.** |

## Where things are

- `custom_components/sb_scheduler/` — the live Phase 0 integration (day-sets).
- `reference/scheduler-component/` — the inherited tree, **deliberately outside
  `custom_components/`** so HACS cannot install a second `scheduler` domain
  alongside the running one. Phase 1 ports `actions.py`, `store.py`,
  `websockets.py` and `switch.py` from here.
- `card/sb-scheduler-card.js` — the edit-only card. Deployed to `/config/www/`
  and registered as a dashboard resource; NOT yet its own HACS repo.
- `tests/test_day_set.py`, `tests/test_timer.py` — run with plain `python3`, no HA needed. It stubs the
  few HA surfaces `day_set.py` touches, so the real evaluation logic is exercised
  offline. 31 checks.

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

## Phase 2 notes (card)

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
- Plain `<input>` elements, not `ha-textfield`: it renders invisible outside
  `ha-form`. Native `<select>` needs explicit `option` colours plus
  `color-scheme: light dark` or its popup ignores the theme.
