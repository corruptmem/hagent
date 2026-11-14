# Spike task-2: own writes through HA context IDs

`probe.py` checks how HA links a WebSocket `call_service` command to the
`state_changed` events that the command causes. An HA context is a record
with an ID that HA attaches to a service call and to its state changes.

The probe creates a temporary `input_boolean` helper with a random name. It
changes only that helper, then deletes it. It prints a redacted JSON report.
`result.json` holds the report from one run against HA Core 2026.11.1.

Run it:

```sh
set -a; . <env file with HASS_URL and HASS_TOKEN>; set +a
uv run --with websockets python spikes/task-2/probe.py
```

Results:

- `call_service` returns the context in its result.
- The event context ID and the new state context ID equal the result
  context ID.
- HA sends the event before the result (seq 15 before 16, 61 before 62).
- A no-op call (`turn_on` on a helper that is on) returns a context and
  causes no event.
- HA rejects an extra `context` field with `invalid_format`.
- A change from another connection has a different context ID.
- `parent_id` is null for direct calls.

`app/src/hagent_app/own_writes.py` turns these results into a pure matcher.
