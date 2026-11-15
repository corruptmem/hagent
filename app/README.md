# hagent app

This package is the hagent app. The app is the container that runs next to
HA Core. It does all agent work (see `docs/design.md` section 6).

Start it with `python -m hagent_app`. The app stops cleanly on SIGTERM and
SIGINT.

## Package layout

| Path | Content |
|---|---|
| `src/hagent_app/__main__.py` | The entry point. It loads the configuration, sets up logging and signal handlers, and runs `App`. |
| `src/hagent_app/app.py` | The composition root. `App` opens the database, starts the HA client and the state cache, then starts each component. |
| `src/hagent_app/config.py` | The configuration: `options.json` plus environment overrides. |
| `src/hagent_app/storage/` | SQLite storage. `database.py` holds the connection and its worker thread. `migrate.py` holds the migration runner. `migrations/` holds the SQL files. |
| `src/hagent_app/ha/` | The connection to HA Core. `client.py` is the WebSocket client. `state_cache.py` is the HA state cache. |
| `src/hagent_app/http/` | The HTTP server component (`HttpServer`), the JSON error format and the change events (task-7). |
| `src/hagent_app/agents/` | Agent definitions, definition versions, private store records and the agents management API (task-7). |
| `src/hagent_app/providers/` | Provider adapters, the price table, cost records and budgets (task-9). See "Providers and cost" below. |
| `src/hagent_app/ingress/` | `IngressGuard`, the source-address check, and `UiComponent`, which serves the UI files (task-18). |
| `src/hagent_app/ha_catalog/` | `CatalogComponent`: the read-only HA catalogue for the UI pickers (task-18). |
| `src/hagent_app/tools/` | The tool executor, the scope check, the HA registry snapshot, the audit log and the scope monitor (task-8). |
| `src/hagent_app/turns/` | The agent turn, the turn history and the running summaries (task-10). See "How to run an agent turn" below. |
| `src/hagent_app/router/` | The router, the persistent invocation queue, the fallback, chat retries and the test invocation route (task-12). See "How to invoke an agent" below. |
| `src/hagent_app/own_writes.py` | `OwnWriteTracker`. It links state changes to the service calls that caused them (task-2). |
| `tests/` | The tests. |
| `tests/helpers/fake_ha.py` | `FakeHA`, a fake HA WebSocket server. Use it in every test that needs HA. |
| `tests/helpers/home.py` | A sample home (registries, services and states) for scope tests. |
| `tests/helpers/tools.py` | `harness`, the tool test harness, with fake knowledge, history and agent-state stores. |
| `tests/helpers/wait.py` | `wait_until`, a poll helper for async tests. |
| `tests/helpers/fake_providers.py` | `FakeProviders`, a fake provider API that replays recorded responses from `tests/fixtures/providers/`. |
| `tests/helpers/providers_env.py` | `ProvidersEnv`, a started providers component on a temporary database. The `providers_env` fixture creates it. |

Each later area adds its own package under `src/hagent_app/`, for example
`agents/`, `tools/`, `providers/` or `scheduler/`. An area does not edit the
modules of another area.

## Terms

- **Component**: one area of the app with its own `start` and `stop`. It
  implements the `Component` protocol in `app.py`.
- **App context**: the `AppContext` object. It holds the shared services:
  `config`, `db`, `ha`, `states` and `installation_id`. Each component
  receives it in `start`.
- **Migration**: an SQL file that changes the database schema.
- **State cache**: the in-memory copy of all HA entity states (design
  section 13.2).

## Runtime choices

- **aiohttp** supplies the WebSocket
  client for HA. It also supplies the HTTP server for the management API
  and the Ingress UI, and the WebSocket server for the integration link.
  HA Core uses aiohttp too.
- **pydantic** validates agent definitions. Pydantic AI depends on it too.
- **sqlite3** from the standard library holds the data. One worker thread
  owns the connection. Async code calls `await db.run(fn)`, where `fn`
  receives the connection. The connection uses `autocommit=True`. Use
  `with transaction(conn):` to group writes.
- **pytest-asyncio** runs the async tests, in `auto` mode.

## How to add a component

1. Create a package, for example `src/hagent_app/scheduler/`.
2. Write a class with a `name` attribute and these two methods:

   ```python
   async def start(self, ctx: AppContext) -> None: ...
   async def stop(self) -> None: ...
   ```

3. Add one line to `default_components()` in `app.py`. `App` starts the
   components in list order and stops them in reverse order.
4. Test the component with `FakeHA` and a temporary data directory. Do not
   use a live HA instance.

`start` must return when the component is ready. It must not wait for HA.
The HA client connects in the background. A component that needs HA can wait
with `await ctx.ha.wait_connected()` in its own task.

## How to add HTTP routes

`HttpServer` serves the management API, and later the UI. It is a
component. A route handler returns JSON. It raises `ApiError` for an error.
The server converts the error to the format in `docs/api.md`.

Add routes and middleware in `default_components()`, before the server
starts. aiohttp freezes the routes when the server starts, so a later call
raises `RuntimeError`:

```python
def default_components() -> list[Component]:
    http = HttpServer()
    agents = AgentsComponent(http.events)
    http.add_routes(agents.routes())
    rules = RulesComponent(http.events)
    http.add_routes(rules.routes())
    return [agents, rules, http]
```

- Keep `http` last in the list. `App` then starts the server after the
  components that serve its routes, and stops it first.
- A handler that runs before its component started returns 503
  `not_ready`. `AgentsComponent.service()` shows the pattern.
- Put routes under `/api/<area>`. Document each route in `docs/api.md`.
- `http.add_middleware(mw)` adds a middleware that runs before every route.
  The JSON error middleware runs before it, so an `ApiError` from the
  middleware becomes a JSON response.
- `IngressGuard` is the first middleware. It refuses each connection that
  does not come from `172.30.32.2` (design section 6.1), also for the event
  WebSocket. It uses the TCP peer address, never a header. Before it
  starts, it refuses all requests with 503.
- The integration link connects from HA Core, not from that address. The
  link must therefore use its own port, or give its path to
  `IngressGuard(exempt_prefixes=[...])` and authenticate each connection
  itself.
- `UiComponent` serves the UI on every path that does not start with
  `api/`. Its route pattern excludes `api/`, so it never hides a route
  that another component adds. Put new routes under `/api/<area>`.
- `http.events` is the change-event hub. Call
  `http.events.publish(topic, action, record_id, data)` after a change.
  The `/api/events` WebSocket sends each event to the UI. An in-process
  consumer, for example the integration link, calls
  `http.events.subscribe(fn)`. `fn` is a plain function. It must be fast.
  Call `publish` on the event-loop thread only, never inside `db.run`.

## How to read agent definitions

`AgentReader` is the read interface for agent definitions. Create it in
the `start` of your component: `AgentReader(ctx.db)`. It has three methods:

- `await reader.list_agents()`: every live agent.
- `await reader.get(agent_id)`: the live agent, or `None`. The result is an
  `Agent` snapshot with `version` and `definition`. Keep the snapshot for
  the whole invocation, so that a change takes effect only between
  invocations (design section 9.6). Record `version` in history.
- `await reader.get_version(agent_id, version)`: one version, also of a
  deleted agent, or `None`.

The definition is an `AgentDefinition` (`agents/definition.py`). The models
are frozen and the lists are tuples. The tool executor (task-8) reads the
scopes from `definition.tools`, the outputs from `definition.outputs`, the
store attachments from `definition.knowledge` and the approval settings
from `definition.approval`.

Agent tools must use only `AgentReader`. They must never receive
`AgentService`, which writes definitions (design section 7). Only the
management API uses `AgentService`.

The app never removes a row from `agents`. A deleted agent has `deleted_at`
set, and its versions stay. A table of another area can refer to
`agents(id)` and to `agent_versions(agent_id, version)` with a plain
foreign key.

## Knowledge store records: hand-off to task-11

Task-7 creates the private store of each agent, so it creates the first
form of the `knowledge_stores` table (migration `0101`). The table has
these columns: `id`, `name` (unique, not case-sensitive), `description`,
`owner_agent_id`, `private` and `created_at`. Each agent has at most one
private store.

Task-11 owns knowledge stores. It extends this table with `ALTER TABLE` in
its own range (0500–0599), for example with the size limit. It adds the
tables for files and documents, and the management API for stores.

Rules that task-7 applies, and that task-11 must keep:

- When the user deletes an agent and keeps its private store, the app sets
  `private = 0` and `owner_agent_id = NULL`. The store is then a normal
  shared store.
- When the user deletes an agent and its private store, the app deletes
  the row from `knowledge_stores`. Task-11 tables that refer to a store
  need `ON DELETE CASCADE`, or the delete must remove their rows first.
- The app refuses to delete a private store that other live agents attach.
- A definition can attach only stores that exist in `knowledge_stores`.

## How to call tools: the executor and the scope check

Every agent tool call goes through `ToolExecutor` (`tools/executor.py`). The
executor checks the scope before a tool reaches HA or a store (design
sections 8 and 19). The app has administrator access to HA, so this check
is the only barrier. It fails closed.

Terms:

- **Tool**: one operation that a model can call, for example
  `ha.call_service`. Each tool belongs to a tool group: `ha`, `knowledge`,
  `memory` or `self`.
- **Registry snapshot**: one copy of the HA entity, device, area, floor and
  label registries and the HA service list (`tools/registry.py`).
- **Prepared call**: a service call that passed the check. Its target names
  explicit entity IDs only, so HA acts on exactly the checked entities.

Use the executor from an agent turn (task-10):

```python
executor = tools_component.executor()
ctx = ToolContext(agent, invocation_id=invocation_id, dry_run=False)
specs = executor.tools_for(agent.definition)  # tool names, texts, schemas
result = await executor.execute(ctx, "ha.call_service", arguments)
```

`agent` is the `Agent` snapshot from `AgentReader`. The executor takes the
agent ID and the scope from the snapshot, never from the arguments.
`execute` never raises for a denial: it returns a `ToolResult` with
`error_code`. With `dry_run`, the executor checks and records a change but
does not make it, and it sends no approval request. The result says
whether the call would need approval.

Steps of `execute`, in order:

1. The definition must turn on the tool. The arguments must validate.
2. `authorize` checks the scope and returns a prepared request.
3. If the definition marks the call for approval, an `ApprovalGate` must
   accept it. With no gate, the executor denies the call. Task-16 sets
   `ToolsComponent.approval`. Service and entity approval applies only to
   tools that write.
4. For a call that changes HA, the executor writes an audit row with status
   `pending`. If that write fails, it does not call HA.
5. `run` acts on the prepared request only. If the invocation is
   cancelled, the executor still writes the final audit status.

Rules of the HA check (`tools/scope.py`):

- Area, device, floor and label targets resolve to entity IDs, also inside
  the scope. Top-level target keys in the service data join the target.
- Known entity and device IDs anywhere in the service data, in values and
  in keys, must be in the scope. The check lower-cases text and splits it
  at commas, as HA does.
- The members of a group entity (the `entity_id` state attribute, also of
  nested groups) must be in the scope too. Groups that an integration
  manages without that attribute, and template entities that run actions,
  are not visible to the check.
- An area, device, floor or label target with no entity of the service
  domain causes a denial, so that HA never gets a call whose target the
  check dropped.
- Templates in the service data, unknown target keys, and unknown devices,
  areas, floors, labels or services cause a denial.
- Indirect services need their exact name in `tools.ha.services`.
- A call that resolves to no entity needs its exact name in
  `tools.ha.services`. This stops `mqtt.publish`, `recorder.purge` and
  similar services.
- Outputs are a short form of `control`, but not for indirect services.
- `admin` passes every call unchanged.
- While the registry is stale, every check that needs it fails.

The admin tools accept only the WebSocket commands in `tools/admin.py`.
Each change command has a reader for the previous version, which the audit
log keeps. If the reader fails, the change does not run. HA changes
automation, script and scene configuration only through REST, and the app
has no REST client yet.

`ScopeMonitor` resolves every scope again after each registry reload. If a
scope grew within the same definition version, it creates an HA persistent
notification and publishes `tools` / `scope_grown`. It stores the last
result in `tool_scope_snapshots`, so it also finds growth across a restart.

### How to plug in a store

The knowledge, memory and self tools use the protocols in
`tools/stores.py`: `KnowledgeStore` (task-11), `HistoryStore` (task-10) and
`AgentStateStore` (task-13 to task-15). Set the store on `ToolsComponent`
in `default_components()`, before the app starts. Until then, those tools
return `unavailable`. The executor passes only
attached store IDs and the invocation's agent ID to a store.

### How to add a tool

1. Subclass `Tool` (`tools/base.py`). Set `name`, `group`, `description`
   and `args_model`. Set `changes_ha` if the tool changes HA, and `writes`
   if it changes HA or a store.
2. Put every check in `authorize`. It must not change anything.
3. Use only the prepared request in `run`.
4. Add the tool to `HA_TOOLS` or `STORE_TOOLS`.
5. Add an out-of-scope case to `CASES` in `tests/test_tool_executor.py`.
   The test fails for a tool without one.

Tools must never receive `AgentService`. `tests/test_tools_no_writer.py`
checks this.

## How to run an agent turn

The `turns` package runs agent turns (design sections 9.5, 11, 13.1 and
13.2). `TurnsComponent` owns a `TurnRunner` and a `TurnHistory`.

Terms:

- **Stable prefix**: the start of the prompt that does not change between
  invocations: the tool definitions and the instructions. The provider
  prompt cache works only for this part.
- **Source key**: the key of a running summary. It is the source of the
  invocation, or `assist:<conversation ID>` for an Assist conversation.
- **Running summary**: a short text that replaces the older records of one
  source key in the prompt. The summary model of the definition writes it.

### Entry point for the router

The router (task-12) calls one method:

```python
result = await turns.run_turn(
    agent,
    TurnInput(
        source="invoke",
        inputs={"sensor.<name>": "19.5"},  # the router collects the inputs
        data={"reason": "..."},  # the caller data
        invocation_id=invocation_id,
        conversation_id=None,  # set for Assist
        result_schema=None,  # an AI Task structure, if any
        dry_run=False,
    ),
)
```

`agent` is the `Agent` snapshot from `AgentReader`. `result` is a
`TurnResult`:

- `status` is `ok`, `stopped`, `failed` or `cancelled`.
- `stop_reason` says why a turn did not return a result:
  `tool_call_limit`, `token_limit`, `request_limit`, `budget`,
  `provider_error`, `invalid_result` or `error`. The router then returns
  the fallback (design section 9.7).
- `result` is the text or the structured result. It is `None` for the
  result type `none`.
- `usage` holds the requests, tool calls and token counts of the turn.

`run_turn` does not raise for a model, provider or limit failure. A
cancelled turn records `cancelled` and raises `CancelledError` again. The
runner does not retry a failed model call. The router decides on retries.

### Prompt order

`PromptBuilder` builds the prompt in the order of design section 9.5:

- Part 1, the tool definitions, comes from `ToolExecutor.tools_for`. Provider tool names
  cannot contain `.`, so `ha.get_state` becomes `ha__get_state`.
- Parts 2 to 4 are the instructions: the instructions of the definition, the attached
  stores (description, access, index sorted by name, main files), and the
  rules, schedules and scopes. This part has no time, inputs or caller
  data.
- Parts 5 to 7 are the user prompt: the running summary and the latest records of the
  source key, the recall results (only if retrieval is on), and the
  invocation (source, time, inputs, caller data, main-file requests).

The builder reads stores and rules only through the tool executor, with the
same scope check as a model tool call.

### Tool calls, limits and compaction

- Each tool call goes to `ToolExecutor.execute`. History keeps the full
  result. The model receives at most `tool_result_max_chars` characters,
  with a marker.
- `UsageLimits` applies `budget.max_tool_calls` and `budget.max_tokens`.
  The executor never runs more calls than the limit.
- `Caching` adds provider cache markers. `WarnOnCacheBusts` warns when the
  cached prefix collapses.
- `TieredCompaction` with `ClearToolResults` clears old tool results when
  the context is larger than `compaction_target_tokens`. Each clear must
  save `compaction_min_clear_tokens`. The runtime uses only local
  strategies, because `MeteredModel` refuses provider-side compaction.
- A structured result must match its JSON schema. The model gets one
  correction attempt. A second failure gives `invalid_result`.
- When an injected `MAIN.md` reaches `main_file_warn_fraction` of
  `main_file_max_chars`, the invocation part asks the agent to move detail
  into other files. The runtime does not cut the file.

### History

`TurnHistory` writes these tables (migration `0400`):

| Table | Content |
|---|---|
| `turns` | One row for each turn: input, prompt, hash of the instructions, status, stop reason, result, messages and usage. |
| `turn_tool_calls` | Each tool call with its full result and its audit row ID. |
| `turn_compactions` | Each compaction: `tool_result_cut`, `in_turn`, `running_summary` (with the new summary) or `main_file_request`. |
| `source_summaries` | The running summary of each source key. |

Read interface for the Inspect screens (task-19):

- `await history.list_turns(agent_id, source_key=..., status=..., before=..., limit=...)`:
  turns, newest first, without messages.
- `await history.get(turn_id)`: one turn with its messages, tool calls and
  compactions.
- `await history.summary(agent_id, source_key)`: the running summary.

`TurnHistory` also implements the `HistoryStore` protocol of the tool
executor. `TurnsComponent` sets `ToolsComponent.history` in its
constructor. `rule_owner` returns `None` until task-13 adds rules. A
`HistoryIndex` (task-11) can index the turns and serve `memory.search`:
set `turns.history.index`. Without an index, search uses a plain text
match. Dry runs do not go into the index, the search or the summaries.

### Options

The `turns` section of `options.json` holds the keys in `TurnsConfig`
(`src/hagent_app/turns/config.py`).

## How to invoke an agent

The `router` package handles every invocation (design sections 9.1, 9.4,
9.6, 9.7 and 18). `RouterComponent` is the only entry point. The scheduler
(task-15) and the integration (task-17: `hagent.invoke`, AI Task and
Assist) call it.

Terms:

- **Invocation**: one request to an agent from one source.
- **Lane**: the queue of one agent, or of one Assist conversation.
- **Path**: how the invocation got its result: `rule`, `agent_turn` or
  `fallback`.
- **Fallback reason**: why the router used the fallback.

### Entry point

```python
handle = await router.submit(
    InvocationRequest(
        agent_id,
        "invoke",  # invoke, ai_task, ai_task_evaluate, schedule, event, review, assist
        text=None,  # optional text; the agent receives it as data["text"]
        data={"reason": "..."},  # the caller data
        conversation_id=None,  # required for assist
        result_schema=None,  # an AI Task structure, if any
        caller={"automation_id": "..."},  # source metadata, for history only
    )
)
result = await handle.result()  # or: result = await router.invoke(request)
```

- `submit` returns at once. `handle.invocation_id` names the invocation.
  A cancelled `handle.result()` does not stop the invocation.
- `await router.cancel(invocation_id)` stops a queued or running
  invocation. Use it for `on_timeout: cancel`. The invocation records
  `cancelled`, and its turn writes nothing more.
- `router.add_result_listener(fn)` calls `fn` with each final result, also
  for invocations that no caller waits for (`wait: false`, or a resumed
  queue entry). Task-17 fires `hagent_result` from it. `fn` must be fast.
- `submit` raises `RouterNotRunningError` before start and during stop.

`InvocationResult` holds `status` (`ok`, `fallback`, `error` or
`cancelled`), `path`, `result`, `fallback_reason`, `error_code`, `error`,
`agent_version`, `rule_id`, `rule_version`, `turn_id` and `cost_usd`.
`result.to_data()` gives the response data of design section 9.2. The
error codes are stable: `agent_not_found`, `agent_off`, `busy`,
`queue_full`, `cancelled`, `no_last_result`, `fallback_error` and
`not_running`.

### Steps

1. `submit` refuses an agent that does not exist or is turned off.
2. `submit` applies the concurrency mode of the current definition:
   - `queued`: the invocation waits in the lane. At most `queue_size`
     invocations wait. One more gets `queue_full`.
   - `single`: if one runs or waits, the invocation gets `busy`.
   - `restart`: the router cancels the running and waiting invocations,
     then starts the new one. They get `cancelled`.
   - `parallel`: the invocation starts at once. At most `queue_size` run
     at the same time. One more gets `queue_full`.
   An Assist invocation uses the lane of its conversation in `queued` mode,
   with `assist_queue_size`. It never waits for background work.
3. When the invocation starts, the router reads the agent again. The
   invocation keeps this snapshot to the end. A change to the definition
   therefore takes effect only between invocations. A turned-off agent
   gets `agent_off` here too.
4. The router collects the input values from the state cache (entities,
   and the entities of the input devices). After an app start, it waits
   up to `state_wait_seconds` for the first state load.
5. The rule stage answers, or says why not. Review, Assist and test
   invocations skip it.
6. The router runs an agent turn. If the turn returns no result, or cannot
   start, the router applies the fallback of the definition.
7. The router sums the cost of the provider calls of the invocation, and
   records the result in `invocations`.

### Fallback

| `stop_reason` of the turn | Fallback reason |
|---|---|
| `budget` | `budget` |
| `provider_error` (after the retries) | `provider_unreachable` |
| `tool_call_limit`, `token_limit`, `request_limit` | the same name |
| `invalid_result` | `invalid_result` |
| `error` | `turn_failed` |
| the turn cannot start | `runtime_unavailable` |
| the app restarted during the run | `app_restart` |
| the app stopped during the run | `app_stopped` |

- `last_result`: the result of the last `ok` invocation with the same
  source key (the source, or `assist:<conversation ID>`). Dry runs and
  fallback results do not count. With no such result, the status is
  `error` with `no_last_result`.
- `fixed`: the value of the definition. The definition has no writes for
  the fallback yet, so the router applies no writes.
- `no_change`: status `fallback` with no result.
- `error`: status `error` with `fallback_error`.

### The rule stage (task-13)

`RouterComponent.rules` is a `RuleStage`. Set it in `default_components()`
before the app starts. Until task-13, `NoRules` returns `NO_RULE_MATCHED`.
`try_rules(RuleRequest)` gets the invocation ID, the agent snapshot, the
request and the inputs. It returns a `RuleOutcome`:

- `answered=True` with `result`, `rule_id` and `rule_version`: the router
  records the path `rule` and starts no turn.
- `answered=False` with a `reason` (`no_rule_matched`, `declined`,
  `rule_error`): the router starts an agent turn. It records `rule_id` and
  `rule_version` of a rule that declined.
- Read the rule set once, at the start of `try_rules`, so that a rule
  change takes effect only between invocations.
- A stage that raises counts as `rule_error`.

### Queue and restart

The `invocations` table (migration `0600`) is the history and the queue.
The router writes a row with status `queued`, sets `running` when the
invocation starts, and sets the final status at the end.

- On start, the router records each `running` row as failed, with the
  fallback and the reason `app_restart`. It runs each `queued` row again,
  in the order of arrival, and gives the result to the result listeners.
- On stop, the router cancels the running invocations. Each records the
  fallback with the reason `app_stopped`. Queued rows stay `queued`. Their
  callers get `not_running`.

### Cost

The router sets `providers.meter.current_invocation`, a context variable,
in each invocation task. `Meter` writes it to
`provider_calls.invocation_id` (migration `0601`). The router sums the cost
when the invocation ends. Tasks that the invocation starts inherit the
value. The running summary update therefore adds cost to the invocation
after its result.

### Chat retries

`RetryingChatModels` is the model factory of `TurnsComponent`. It wraps
each `MeteredModel` in a `RetryingModel`. The SDK clients do not retry
(`max_retries=0`). The router retries one model request, never a whole
turn, because a turn can change HA before a request fails. Each attempt
goes through the meter. Retryable errors: connection errors, timeouts,
HTTP 429 and HTTP 5xx. A spent budget, a missing key, an unknown price, a
spent quota and a rejected request are not retryable. Streamed requests
are not retried.

### Test invocation

`router.test(agent_id, definition=..., text=..., data=..., states=...)`
runs one dry-run turn outside the queue. `POST api/agents/{id}/test` calls
it (see `docs/api.md`). The sample states go to the turn through
`TurnsComponent.run_turn(..., states=...)` and
`ToolExecutor.with_states`, so the HA read tools see them too.

### Options

The `router` section of `options.json` holds the keys in `RouterConfig`
(`src/hagent_app/router/config.py`).

## How to use the HA client and the state cache

- `await ctx.ha.call_service(domain, service, data, target=...)` returns a
  `ServiceResult` with the `context` that HA created. Its `context_id` links
  the call to its `state_changed` events. Give it to `OwnWriteTracker`.
- `await ctx.ha.subscribe_events(handler, event_type)` registers a
  subscription. The client subscribes again after each reconnect. The
  handler is a plain function. It must be fast and must not block.
- `ctx.ha.add_connect_listener(fn)` calls the async `fn` after each connect.
- `ctx.states.get(entity_id)` returns a `State` or `None`. The cache keeps
  its data while HA restarts. `ctx.states.stale` is true while the data may
  not match HA.

The client does no scope check. Agent tools must call HA only through the
tool executor, which checks the scope first.

## How to add a migration

A migration file has the name `NNNN_<name>.sql`. `NNNN` is a four-digit
version number. `<name>` uses lower-case letters, digits and underscores.
Put the file in `src/hagent_app/storage/migrations/`.

Each area owns a range of version numbers. Use the next free number in the
range of your area:

| Range | Area | Task |
|---|---|---|
| 0001–0099 | Core | task-6 |
| 0100–0199 | Agents and versions | task-7 |
| 0200–0299 | Tool audit | task-8 |
| 0300–0399 | Providers and cost | task-9 |
| 0400–0499 | Turns and history | task-10 |
| 0500–0599 | Knowledge | task-11 |
| 0600–0699 | Router and queue | task-12 |
| 0700–0799 | Rules | task-13, task-14 |
| 0800–0899 | Scheduler | task-15 |
| 0900–0999 | Approvals | task-16 |

The ranges do not set the merge order. Version 0100 can merge before version
0002. The runner therefore applies every version that is not in the
`schema_migrations` table, in numeric order. It does not apply only the
versions above the highest applied version.

Rules for a migration file:

- Do not change a migration file after it merges. Add a new file instead.
- Do not write `BEGIN`, `COMMIT` or `ROLLBACK`. The runner applies each file
  and its `schema_migrations` row in one transaction. A failed file leaves
  no tables and no row.
- Create only the tables of your area. Use `STRICT` tables.
- A foreign key to a table of another area needs that table to exist first.
  Give your file a higher number than the file that creates that table.

The app runs the migrations on each start, in `Database.open`. Two files
with the same number stop the start with `MigrationError`.

## How to add configuration keys

The Supervisor writes the app options to `/data/options.json`. The core keys
are in `Config`: `port` and `log_level`. Environment variables override
them (see `load_config`).

Do not add fields to `Config`. Put the keys of your area in one JSON object
under the name of your area, and read them with `Config.section`:

```python
@dataclass(frozen=True)
class SchedulerConfig:
    min_interval: int = 60


config = ctx.config.section("scheduler", SchedulerConfig)
```

Missing keys take the defaults of the dataclass. Unknown keys cause
`ConfigError`. Task-21 adds the keys to the app option schema when it
packages the app.

## Providers and cost

The `providers` package makes all model calls. It has one interface for
each call type:

| Call type | Interface | Implementation |
|---|---|---|
| Chat | A Pydantic AI `Model` | `ProvidersComponent.chat_model(provider, model, agent_id=...)` returns a `MeteredModel` around `OpenAIResponsesModel` or `OpenRouterModel`. |
| Decision | `DecisionClient.decide(request, agent_id=...)` | `DecisionAdapter`. It calls `POST /v1/decisions` (OpenAI) or `POST /api/alpha/decisions` (OpenRouter) directly. |
| Embedding | `EmbeddingClient.embed(provider, model, texts, agent_id=...)` | `EmbeddingAdapter`. It calls `POST <base>/embeddings` on either provider. |

`agent_id=None` charges a call to the installation only, for example a
document embedding.

Terms:

- **Price table**: `PriceTable`. It computes a cost from token counts. The
  first layer that knows the model wins: the `price_overrides` option, then
  the daily copy of the OpenRouter price lists, then genai-prices (the
  price library of Pydantic AI).
- **Reservation**: the expected cost of a call, held against the budgets
  before the call starts. Parallel calls see each other's reservations.
- **Meter**: `Meter`. It reserves, records each call in `provider_calls`,
  and deletes the reservation.

Rules:

- Each call records input, cache-read, cache-write and output tokens, and
  the cost. For OpenRouter the cost is `usage.cost` from the response. For
  OpenAI it comes from the price table. For OpenRouter calls the meter also
  stores the computed cost and the drift between the two.
- Money columns hold integer pico-USD (1e-12 USD).
- The budget day is the local date of the app. The Supervisor sets the time
  zone of the app to the time zone of HA, so budgets reset at local
  midnight in the home. `Meter` takes a `clock` argument for tests.
- A call with no known price fails with `PriceUnknownError` before any
  HTTP request. Set `unknown_price_reserve_usd` to reserve a fixed amount
  instead.
- The adapters do not retry. They raise `ProviderHTTPError` with a
  `retryable` flag. The runtime decides on retries.
- The keys are in the `providers` section of `options.json`, or in
  `HAGENT_OPENAI_API_KEY` and `HAGENT_OPENROUTER_API_KEY`. Code reads them
  through a `KeySource`. Task-20 replaces the source with the settings
  store. A key goes only into the `Authorization` header of a provider
  request. Error texts pass through `redact`.
- The daily OpenRouter price refresh starts 60 seconds after app start.
  Tests set `refresh_openrouter_prices` to `false`.

The `providers` option keys are in `ProvidersConfig`
(`src/hagent_app/providers/config.py`).

The test fixtures are real responses with redacted IDs. Chat and embedding
fixtures come from `spikes/task-9/record_fixtures.py`. Decision fixtures
come from `spikes/task-5/samples/`.
## Ingress options

The `ingress` section of `options.json` has two keys (see
`docs/api.md`, "Ingress options"):

- `allowed_addresses`: the addresses and CIDR networks that can connect.
  The default is `["172.30.32.2"]`. For local development, set it to
  `["127.0.0.1", "::1"]`. The app logs a warning when the list differs
  from the default.
- `ui_dir`: the directory of the UI build. An empty value selects
  `frontend/dist` in the source checkout. See `frontend/README.md`.

Tests that connect to an app from `default_components()` must allow
`127.0.0.1` in their `Config`. `test_ingress.py` shows the pattern.

## Development settings

Outside an app container, set these environment variables:

| Variable | Meaning | Default |
|---|---|---|
| `HAGENT_DATA_DIR` | The data directory. | `/data` |
| `HAGENT_HA_URL` | The HA WebSocket URL. | `ws://supervisor/core/websocket` |
| `HAGENT_HA_TOKEN` | The access token. | the value of `SUPERVISOR_TOKEN` |
| `HAGENT_PORT` | The HTTP port. | `8099` |
| `HAGENT_LOG_LEVEL` | The log level. | `INFO` |
| `HAGENT_OPENAI_API_KEY` | The OpenAI API key. | the `providers` option |
| `HAGENT_OPENROUTER_API_KEY` | The OpenRouter API key. | the `providers` option |

Never commit a token or an `.env` file.
