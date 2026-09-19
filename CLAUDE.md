# sb-scheduler-component — session notes

Fork of **nielsfaber/scheduler-component** (backend), created 2026-09-19 because
upstream's backend is effectively dormant: last release v3.3.8 (Nov 2024), one
code commit in 2026. The *card* (nielsfaber/scheduler-card) is actively
maintained by a contributor and is NOT forked — this fork keeps the `scheduler`
domain, websocket API and storage format so the stock card works unchanged.

| | |
|---|---|
| Repo | github.com/snadboy/sb-scheduler-component — local `~/projects/git/sb-scheduler-component` |
| Upstream | `git remote add upstream https://github.com/nielsfaber/scheduler-component.git` (already set) |
| Release | `v3.3.8-sb.1` = upstream main (incl. "add reload_storage action") + the two fixes |
| HACS | custom repo id **1377310720**, category integration. Upstream repo was REMOVED from HACS to avoid it clobbering the fork on a future upstream release. |

## What the fork changes

1. **Workday sensor is configurable.** Upstream hard-codes
   `const.WORKDAY_ENTITY = "binary_sensor.workday_sensor"` with no options flow
   at all (upstream config_flow.py is 26 lines). Added `SchedulerOptionsFlow`
   (`workday_entity` entity selector + `notify_on_failure` boolean),
   `const.workday_entity(hass)` / `const.notify_on_failure(hass)` helpers reading
   entry options, a WARNING when the sensor is missing, and
   `SchedulerCoordinator.async_rearm_workday_sensor()` wired to an update
   listener so a change takes effect without a restart.
   Upstream #382 asked for this and was closed by the stale bot; Niels' objection
   was that Scheduler also needs the sensor's `workdays` attribute for look-ahead.
2. **Missed/failed actions are loud.** THE REAL SILENT FAILURE (upstream #534) is
   `ActionQueue.is_available()`: when the target entity or the action is
   unavailable it logs at **DEBUG** and `async_process_queue` returns — the
   schedule looks like it ran. Now the reason is recorded and reported once per
   occurrence: ERROR log + `scheduler_action_failed` event
   (`schedule_id`/`action`/`entity_id`/`reason`) + optional persistent
   notification. The queue still waits and retries when the target returns.
   Also wrapped the actual dispatch (`async_execute_task`) with an unknown-action
   check and try/except. Dispatch stays NON-blocking (blocking=True would let a
   long `script.turn_on` stall the queue).

## Gotchas learned here

- **Execution lives in `ActionQueue`, not `ActionHandler`.** First cut put the
  reporting methods on ActionHandler; `async_process_queue` is on ActionQueue,
  so `self.async_execute_task` would have raised AttributeError. Check the class
  that owns the method before adding helpers.
- **`gh release create` in a fork targets the PARENT repo.** It refused with
  "tag ... has not been pushed to nielsfaber/scheduler-component". Always pass
  `--repo snadboy/sb-scheduler-component`.
- **HA core logs are not in `docker logs homeassistant`** and `/config/home-assistant.log`
  does not exist here; `ha core logs` over SSH returns 401. What works:
  `docker exec homeassistant sh -c 'TOKEN=$(cat /run/s6/container_environment/SUPERVISOR_TOKEN); curl -s -H "Authorization: Bearer $TOKEN" http://supervisor/core/logs?lines=20000'`
  (MQTT debug floods it — grab a big window and grep).
- **Persistent notifications are not entities any more.** Checking
  `persistent_notification.*` states shows nothing; use the WS command
  `persistent_notification/get`. Dismiss via the `persistent_notification.dismiss`
  service (the WS `dismiss` command does not exist).

## Verified live (2026-09-19)

Options flow renders both fields and saves; changing options re-arms the tracker
with no restart and no disturbance to running schedules. A test schedule aimed at
a nonexistent entity produced the ERROR log and the persistent notification. The
three real schedules (bedside lamps `workday`, garden irrigation, garden lights)
kept their config and next-trigger times across upstream→fork swap and the HACS
re-install. Backups of the pre-swap storage + upstream code:
`/config/backups_scheduler_20260919_102615/`.

## If upstream ever revives

`git fetch upstream && git merge upstream/main` — the diff is small and confined
to const.py, config_flow.py, __init__.py, actions.py (+ translations/en.json).
