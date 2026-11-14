---
id: TASK-6
title: >-
  App skeleton: configuration, SQLite storage, HA WebSocket client and state
  cache
status: In Progress
assignee:
  - '@claude'
created_date: '2026-11-14 23:03'
updated_date: '2026-11-14 23:31'
labels:
  - app
dependencies:
  - TASK-1
  - TASK-2
references:
  - docs/design.md
priority: high
type: feature
ordinal: 6000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Create the runnable foundation of the hagent app: configuration loading, SQLite storage with migrations, a WebSocket client that connects to HA Core through the Supervisor proxy with SUPERVISOR_TOKEN, and the HA state cache fed by state-change subscriptions. Define the SQLite schema as part of this task.

Design: docs/design.md sections 6, 7 and 13.2.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The app starts, connects to HA through the Supervisor proxy and reconnects after an HA restart
- [x] #2 The state cache holds current entity states and updates on state changes
- [x] #3 SQLite schema migrations run on start and are tested
- [x] #4 Tests run without a live HA instance, using a fake WebSocket server
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Add aiohttp as the one runtime dependency (WebSocket client, later HTTP server, fake server base). Add pytest-asyncio to dev.
2. config.py: read /data/options.json, apply HAGENT_* environment overrides, default HA URL ws://supervisor/core/websocket with SUPERVISOR_TOKEN. Later modules read their own section from the raw options.
3. storage/: Database (stdlib sqlite3, WAL, foreign keys, busy timeout, one worker thread) and a migration runner. Numbered SQL files with reserved ranges per area. The runner applies every unapplied version in numeric order, rejects duplicate numbers, and runs each file plus its version row in one transaction.
4. 0001 core migration: app_info table (installation ID, first start). Later tasks own their tables.
5. ha/client.py: HA WebSocket client. Auth, command futures, call_service with result context, subscriptions that survive reconnect, capped backoff, non-blocking event dispatch, pending futures fail on disconnect.
6. ha/state_cache.py: subscribe first, then get_states, then replay buffered newer events. Keep data during a disconnect; expose ready and stale flags.
7. app.py and __main__.py: component protocol, AppContext, SIGTERM shutdown.
8. Test helper: reusable fake HA WebSocket server with command log and restart simulation. Tests for config, migrations, client, cache and a full app start that survives a restart.
9. app/README.md in STE: layout, migration ranges, how to add a module, migration and config keys.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
## Decisions

Runtime dependency: aiohttp (>=3.13). One package supplies the HA WebSocket client, the HTTP server for the management API (task-7) and the Ingress UI, and the WebSocket server for the integration link (task-17). HA Core uses aiohttp too. The test fake server also uses it. Rejected: websockets plus Starlette plus uvicorn, which is three packages for the same work.

Storage: the standard-library sqlite3 module. One worker thread owns the connection. Async code calls await db.run(fn). The connection uses autocommit=True, WAL mode, foreign keys on and a 5 s busy timeout. aiosqlite is not necessary.

Test runner: pytest-asyncio 1.5 in auto mode (dev dependency).

## Migrations

Files: app/src/hagent_app/storage/migrations/NNNN_<name>.sql. Each area owns a range: 0001-0099 core, 0100 agents, 0200 tool audit, 0300 providers, 0400 turns, 0500 knowledge, 0600 router, 0700 rules, 0800 scheduler, 0900 approvals. app/README.md has the table.

The ranges do not set the merge order. The runner applies every version that is not in schema_migrations, in numeric order. It does not use the highest applied version. It rejects two files with the same number. It applies each file and its schema_migrations row in one transaction. Migration files must not contain BEGIN, COMMIT or ROLLBACK.

0001_core creates only app_info (key, value). The app writes installation_id and created_at on the first start. Sections 6, 7 and 13.2 give the core no other persistent data: the state cache is in memory, and the link secret belongs to task-17.

## API for later tasks

- AppContext: config, db, ha (HAClient), states (StateCache), installation_id. A component implements start(ctx) and stop(), and goes into default_components() in app.py.
- Config.section(name, cls) reads the options of one area into its own dataclass. Nobody edits Config.
- HAClient.call_service(...) returns ServiceResult with context and context_id, for OwnWriteTracker (task-15).
- HAClient.subscribe_events(handler, event_type) registers a subscription that survives reconnects. Handlers are plain functions and must not block. The client does no scope check.
- HAClient.add_connect_listener / add_disconnect_listener.
- StateCache.get(entity_id) returns State or None. The cache keeps its data while HA restarts. stale is true when the client has no connection or the last load did not finish.
- app/tests/helpers/fake_ha.py: FakeHA with auth, get_states, subscribe and unsubscribe, call_service (turn_on, turn_off, toggle; events before the result, same context), custom handlers, a command log (FakeHA.commands) for scope tests, and restart()/up(). The fake shapes match spikes/task-2/result.json, including no event for a no-op turn_on.

## Verification

mise run check passes: ruff, mypy strict (23 files), pytest 47 passed. The suite ran 5 times without a failure.

- AC #2: test_state_cache.py: test_initial_load, test_updates_adds_and_removes, test_service_call_updates_cache, test_keeps_data_while_ha_restarts_then_reloads, test_events_held_during_load_merge_by_time, test_failed_reload_leaves_cache_stale.
- AC #3: test_migrations.py: test_database_open_runs_migrations (migrations run in Database.open on each start), test_numeric_order_and_late_lower_version, test_failed_migration_leaves_nothing, test_bad_migration_files. test_app.py: test_installation_id_is_stable. A built wheel contains 0001_core.sql.
- AC #4: every test uses FakeHA. No test needs a live HA instance.
- AC #1: not checked. test_main.py starts python -m hagent_app as a process against FakeHA. The process connects, reconnects after a simulated HA restart, loads the states again, and exits 0 on SIGTERM. test_config.py proves that the default URL is ws://supervisor/core/websocket with SUPERVISOR_TOKEN. Nobody ran the app through the real Supervisor proxy. That needs the packaged app (task-21).

## Proposed follow-ups (not created)

- task-21: set homeassistant_api: true in the app config, or the Supervisor proxy refuses the connection. Verify AC #1 of this task on a real Supervisor.
- task-21: the database uses WAL mode, and HA backups include /data. A hot backup can copy hagent.db and hagent.db-wal out of step. Choose a cold backup mode, or add a WAL checkpoint before backup.
- task-1 follow-up still open: pin sqlite-vec when task-11 starts.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Built the app skeleton: config loading (options.json plus HAGENT_* overrides, Supervisor proxy URL and SUPERVISOR_TOKEN by default), SQLite storage with a range-based migration runner and a 0001 core migration, a reconnecting HA WebSocket client (call_service returns the context, subscriptions survive reconnects), the HA state cache, a component protocol in app.py, SIGTERM shutdown, a reusable FakeHA test server and app/README.md. Chose aiohttp as the only runtime dependency. Verified with mise run check (47 tests) and a process test of python -m hagent_app against FakeHA. AC #2-#4 checked. AC #1 is open: the real Supervisor proxy path is not tested; task-21 can verify it. Commits f3e5921, 7a815c0.
<!-- SECTION:FINAL_SUMMARY:END -->
