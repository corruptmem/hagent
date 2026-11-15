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
| `src/hagent_app/knowledge/` | Knowledge stores, documents, the search index, the embedding queue and the knowledge management API (task-11). See "Knowledge stores and search". |
| `src/hagent_app/tools/` | The tool executor, the scope check, the HA registry snapshot, the audit log and the scope monitor (task-8). |
| `src/hagent_app/settings/` | `SettingsComponent`: the settings store, the API keys and the settings API (task-20). See "Settings" below. |
| `src/hagent_app/approvals/` | `ApprovalsComponent`: the approval gate of the tool executor, the notifier, the answer listener and the approvals API (task-16). See "Approvals" below. |
| `src/hagent_app/turns/` | The agent turn, the turn history and the running summaries (task-10). See "How to run an agent turn" below. |
| `src/hagent_app/rules/` | The rule spec, the template sandbox, the rule store and the rule engine (task-13). See "Rules" below. |
| `src/hagent_app/backtest/` | Invocation records, backtests, trials and the rule routes of the management API (task-14). See "Backtests and trials" below. |
| `src/hagent_app/router/` | The router, the persistent invocation queue, the fallback, chat retries and the test invocation route (task-12). See "How to invoke an agent" below. |
| `src/hagent_app/scheduler/` | `SchedulerComponent`: timers, cron schedules, reviews, event subscriptions and own-write filtering (task-15). See "Schedules and subscriptions" below. |
| `src/hagent_app/inspection/` | `InspectComponent`: the read-only inspect routes (invocations, traces, history search, audit log, permissions, schedules, cost) and the `invocations` change events (task-19). See "Inspect routes" below. |
| `src/hagent_app/link/` | `LinkComponent`: the integration link WebSocket, its shared secret and the Supervisor discovery (task-17). See "The integration link" below and `docs/link.md`. |
| `src/hagent_app/own_writes.py` | `OwnWriteTracker`. It links state changes to the service calls that caused them (task-2). |
| `tests/` | The tests. |
| `tests/helpers/fake_ha.py` | `FakeHA`, a fake HA WebSocket server. Use it in every test that needs HA. |
| `tests/helpers/home.py` | A sample home (registries, services and states) for scope tests. |
| `tests/helpers/tools.py` | `harness`, the tool test harness, with fake knowledge, history and agent-state stores. |
| `tests/helpers/knowledge.py` | `knowledge_env`, a started knowledge component with a real tool executor, and `FakeEmbeddings`, a fake embedding model. |
| `tests/helpers/pdf.py` | `make_pdf`, which builds small PDF files with or without a text layer. |
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
- The integration link connects from HA Core, not from that address.
  `default_components()` gives its exact path, and only that path, to
  `IngressGuard(exempt_paths=LinkComponent.exempt_paths)`. Subpaths of the
  link path are not exempt. The link checks the shared secret itself. Do
  not add other exempt paths.
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

## Knowledge stores and search (task-11)

The `knowledge` package owns knowledge stores, their files and documents,
and the search index of all collections (design sections 13.3 and 13.4).
Migration `0500` extends `knowledge_stores` with `size_limit` and
`updated_at`, and adds the other tables.

Terms:

- **Collection**: one kind of indexed text: `knowledge`, `history`,
  `conversation` or `entity`.
- **Passage**: one piece of indexed text, at most 1500 characters. Each
  passage has a content hash (SHA-256 of its text).
- **Hybrid search**: an FTS5 keyword search and a sqlite-vec vector search
  on the same query, merged with reciprocal rank fusion (RRF).

Modules:

| Module | Content |
|---|---|
| `service.py` | `KnowledgeService`: stores, files with versions and history, documents, size limits. It implements the `KnowledgeStore` protocol of the tools. |
| `index.py` | `SearchIndex`: the passage table, the FTS5 query, the RRF merge and the keyword-only fallback. |
| `vectors.py` | Every sqlite-vec statement. Vectors are float32 BLOBs in `search_embeddings`. A query uses `vec_distance_cosine` over the passages in scope. It does not use `vec0` KNN tables, because KNN applies its limit before the scope filter. |
| `embedder.py` | The background embedding queue. |
| `text.py` | Extraction of PDF (text layer, with pypdf), Markdown, plain text and HTML, and the split into passages. |
| `entities.py` | `EntityCatalog`: the HA entity catalogue in the index. |
| `history.py` | `HistoryIndex`: the history and conversation index for task-10. |
| `context.py` | `build_turn_context`: the knowledge part of each agent turn. |
| `api.py` | The management routes under `/api/knowledge` (see `docs/api.md`). |

Rules:

- The executor checks attachments before it calls the service. The service
  receives only attached store IDs.
- Each search applies the scope filter in SQL, before the ranking and the
  limit.
- The vector search drops matches with a cosine distance above
  `knowledge.max_vector_distance` (0.75 by default). Without this cutoff,
  every passage in scope would rank. The right value depends on the
  embedding model.
- The app loads sqlite-vec from the component, through `db.run`. No
  migration uses it. If it does not load, if no embedding model is set, or
  if the provider fails, search uses keywords only. `GET
  api/knowledge/status` gives the reason.
- The embedder embeds each content hash once per model. It charges text
  that an agent writes to that agent, and documents and the entity
  catalogue to the installation. A query embedding of `knowledge.search`
  or `memory.search` counts towards the invocation agent:
  `KnowledgeStore.search` receives the agent ID from the executor. A query
  embedding of `ha.find_entities` also counts towards the agent:
  `EntitySearch.rank_entities` receives `agent_id`. A search
  from the management API counts towards the installation. A query that is
  in the query cache costs nothing. A model change re-indexes every passage. The embedder deletes vectors of
  other models and vectors whose text no longer exists.
- If the budget of an agent refuses a batch (`BudgetExceededError` with
  scope `agent`), the embedder skips the text of that agent for the rest of
  the drain. It continues with other text, and tries again after
  `knowledge.embed_retry_seconds`. `GET api/knowledge/status` lists these
  agents in `budget_skipped_agents`. An installation budget error stops
  the drain, as other provider errors do.
- The version history is bounded (`_prune_history`). Each change keeps the
  newest version of each path and at most `MAX_FILE_VERSIONS` (50)
  versions of one path. The text of earlier versions stays at or below the
  store size limit. The oldest versions go first.
- Store errors (`KnowledgeError`) subclass `StoreError` in
  `tools/stores.py`. The executor returns their code to the model, for
  example `version_conflict`.
- Tables that refer to a store use `ON DELETE CASCADE`. Triggers keep the
  FTS5 index in step, also for cascade deletes.

### Knowledge in agent turns (task-10)

`default_components()` connects the knowledge component to the turns
component:

- `turns.history.index = knowledge.history_index`. `HistoryIndex`
  implements the `HistoryIndex` protocol of `turns/history.py`. The turn
  history indexes each finished turn (`index_history`) or Assist turn
  (`index_conversation`), but not dry runs. `memory.search` then calls
  `search_history`, which ranks with hybrid search and sees only the
  records of the invocation agent. The filters are `source` (the source or
  the source key), `since` and `until`.
- `turns.knowledge_context = knowledge.turn_context`. The prompt builder
  calls it for part 3 of each turn (design section 9.5), with
  `turns.index_max_entries` as the index size (50 by default) and
  `turns.main_file_max_chars` as the main file limit (16,000 characters by
  default). It gives the store list, the name-sorted index and the
  injected main files. The service reads at most the limit of each main
  file. A note after the block gives the full length. The
  text holds no time or other value that changes between turns. The
  builder also uses it for the main file requests (design section 13.1).
  If the call fails, the builder reads the stores through the executor.
- Store text is data, not instructions (design section 19). Agents with
  `read_write` access write it, and other agents read it. The knowledge
  part starts with a fixed statement of this rule (`DATA_NOTE`). Each
  injected main file is a fenced data block (`data_block`) with a line that
  names the store, the path and the last author. The fence has more
  backticks than the longest run in the text, so the text cannot close the
  block. The executor fallback and the recall part use the same rule.
- Other code can index records with `await knowledge.history.add(...)` and
  remove them with `remove`, for example when retention deletes a record.

### Hand-off to task-20: knowledge screens

All routes are in `docs/api.md`, "Knowledge stores". A store list with the
attached agents, the items, the file history with restore, the document
upload and the embedding model setting are available. Attachments are part
of the agent definition.

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
  `tools.ha.services`. This stops `conversation.process` and similar
  services.
- The service metadata from `get_services` is an allowlist. A call with
  target entities needs a declared entity target that accepts the domain of
  each entity (`target_not_declared`). Each data key must be a declared
  field (`unknown_field`). Sections of fields count as fields. Missing
  metadata causes a denial (`service_metadata`).
- An explicit target entity of another domain than the service needs the
  exact service name in `tools.ha.services` (`cross_domain`). A wildcard
  such as `remote_bridge.*` does not permit it.
- `group.set` and `group.remove` act on `group.<object_id>`. That entity
  must be in the scope.
- Outputs are a short form of `control`, but not for indirect services and
  not for a service of another domain.
- `admin` passes every call unchanged. For approval, the executor finds the
  entities of an `admin` call: targets, entity and device IDs in the data,
  and group members. If that scan cannot finish (for example a template in
  the data), and `approval.entities` is set, the call needs approval.
- While the registry is stale, every check that needs it fails.
- While the state cache is not loaded or not in sync, `ha.call_service`,
  `ha.get_state` and `ha.find_entities` fail (`states_unavailable`). The
  group check and the data scan read the cache.
- An unexpected error in a check causes a denial (`check_failed`) and an
  audit row. The log keeps the traceback.

Rules of the `self` tools: each spec kind has a strict model, and unknown
keys fail (`invalid_spec`). A state subscription must name entities in the
read scope. A schedule spec is a timer or a cron schedule (see "Schedules
and subscriptions"). A rule spec has the checks in "Rules" below.

The admin tools accept only the WebSocket commands in `tools/admin.py`.
Each change command has a reader for the previous version, which the audit
log keeps. If the reader fails, the change does not run. HA changes
automation, script and scene configuration only through REST, and the app
has no REST client yet.

`ScopeMonitor` resolves every scope again after each registry reload. If a
scope grew within the same definition version, it creates an HA persistent
notification and publishes `tools` / `scope_grown`. It stores the last
result in `tool_scope_snapshots`, so it also finds growth across a restart.
After growth, it stores the new result only when HA accepts the
notification. Otherwise the next reload sends it again.

### How to plug in a store

The knowledge, memory and self tools use the protocols in
`tools/stores.py`: `KnowledgeStore` (task-11), `HistoryStore` (task-10) and
`AgentStateStore` (task-13 to task-15). Set the store on `ToolsComponent`
in `default_components()`, before the app starts. Until then, those tools
return `unavailable`. The executor passes only
attached store IDs and the invocation's agent ID to a store.

`ToolsComponent.entities` takes an `EntitySearch` (task-11 sets it).
`ha.find_entities` first builds the candidates in the read scope. Then it
asks `EntitySearch` to rank only those candidates. Without it, or when the
catalogue is empty, the tool uses its word match.

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

## Approvals

`ApprovalsComponent` (`approvals/`) supplies `ToolsComponent.approval`.
`default_components()` sets it. The approvers, the notify-service
overrides and the time limit come from the settings (task-20). When the
user set no approval settings, the component reads the `approvals` option
section. A settings change applies to the next request. The executor calls the gate for each call
that the definition marks for approval (design section 8).

Terms:

- **Approval request**: one held call, with a random 128-bit ID. The table
  `approval_requests` holds it.
- **Action event**: the `mobile_app_notification_action` event. The HA
  Companion app fires it when a person taps an action button. Its
  `context.user_id` is the HA user of the phone registration.

Rules:

- The gate returns `ApprovalOutcome`. The executor runs the call only for
  an accepted outcome. Denial codes: `approval_rejected`,
  `approval_timeout`, `approval_unavailable` and `approval_stale`. If the
  invocation is cancelled during the wait, the executor records
  `approval_cancelled` in the audit log.
- After an accept, the gate loads the agent again. If the agent is deleted,
  off, or has another definition version, the gate denies the call with
  `approval_stale`. An `agents` change event also ends the pending
  requests of that agent with status `cancelled`.
- An answer counts only before `expires_at`, also when the wall clock
  jumps.
- The API trusts `X-Remote-User-Id` only from the Supervisor peer
  (`172.30.32.2`), unless `approvals.dev_trust_user_header` is on.
- No agent can subscribe to `call_service` events, at any level. Their
  data holds the action IDs of approval notifications.
- Only users in `approvals.approvers` can answer. An answer from another
  user, from no user, or from the HA user of the app has no effect. The
  table `approval_answers` records each answer and its outcome.
- The app listens only to the action event. It ignores the `ios.*` events,
  so that one tap never counts twice.
- One conditional `UPDATE ... WHERE status = 'pending'` decides a request.
  The first valid answer wins. A timeout, a cancel and the app stop use
  the same statement.
- If no notification reaches an approver, the request fails at once.
- After a request ends, the app clears the notification on each target.
- On start, the app expires each pending request.
- A dry run sends no approval request. It stops before the approval hook
  for each tool that needs approval, also for a read tool.

`Notifier` is the send interface. `MobileAppNotifier` is the app-side
implementation. The app cannot read the HA user or the webhook ID of a
phone registration, so it uses the `person` entity of the approver and its
mobile app device trackers, or the `notify_services` setting. It refuses a
service that does not exist, or a device name that more than one mobile app
device has. Set `approvals.notifier` before start to use another sender,
for example one in HA Core (task-17).
## Settings

The `settings` package owns the settings that the user changes in the
management UI (design section 7.2): API keys, budgets, price overrides,
the genai-prices auto-update, decision models and approvers. Migration
`1000` adds the table `app_settings`. Each row is one setting as JSON.

Terms:

- **Stored value**: the value in `app_settings`, or `None`.
- **Default**: the value from the app options. A setting with no stored
  value uses it, so an existing `options.json` keeps working.

`SettingsComponent` must start before `ProvidersComponent` and
`ApprovalsComponent`. Other areas use these parts:

- `settings.keys`: the `KeySource` of the providers. It reads the key on
  each call.
- `settings.providers_config(base)`: the `providers` option section with
  the stored values in place.
- `settings.approvals()`: an `Approvals` object, or `None`.
- `settings.subscribe(fn)`: `fn(names)` runs after each change, on the
  event loop. It must be fast.

Rules:

- No response, error text, change event or log line holds an API key. The
  API shows only a hint (the last four characters of a key with at least
  20 characters).
- `PATCH api/settings` validates every value before it stores any value.
- The genai-prices auto-update fetches from GitHub. It is off by default.
  Tests replace `UpdatePrices` with `ProvidersComponent(price_updater=...)`.

The UI embedding model is not part of this package. The knowledge area
owns it (`api/knowledge/settings`).

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
  into other files. The runtime does not change the file. The prompt holds
  at most `main_file_max_chars` characters of it.

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
`HistoryIndex` (task-11) indexes the turns and serves `memory.search`:
`default_components()` sets `turns.history.index`. Without an index,
search uses a plain text match. Dry runs do not go into the index, the search or the summaries.

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

`RouterComponent.rules` is a `RuleStage`. `default_components()` sets it to
`RulesComponent` (see "Rules"). `NoRules` is the stage for tests that need
no rules. `try_rules(RuleRequest)` gets the invocation ID, the agent
snapshot, the request and the inputs. It returns a `RuleOutcome`:

- `answered=True` with `result`, `rule_id` and `rule_version`: the router
  records the path `rule` and starts no turn.
- `answered=False` with a `reason` (`no_rule_matched`, `declined`,
  `rule_error`): the router starts an agent turn. It records `rule_id` and
  `rule_version` of a rule that declined or failed.
- The stage reads the rule set once, at the start of `try_rules`, so that a
  rule change takes effect only between invocations.
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

## Rules

The `rules` package holds the rule engine (design section 10).
`RulesComponent` owns the rule store and the rule engine. List it after
`tools` and before `router`, because the router runs queued invocations as
soon as it starts.

Terms:

- **Rule spec**: the strict model of one rule version (`rules/spec.py`).
  Unknown keys fail.
- **Rule version**: one spec that never changes. A change adds a version.
- **Rule template**: a sandboxed HA-style template. See
  `docs/rule-templates.md` for the supported subset and the limits.

### Spec

| Key | Content |
|---|---|
| `name`, `description` | Text. |
| `match` | A template. The rule applies when it renders true. |
| `context` | Up to 10 items: `history` (entity statistics over `hours`), `action_response` (an action that only returns data), `store_file` (a file of an attached store) or `invocation_history` (recent invocations from the same source). Each item has a `name` and `cache_seconds`. |
| `action` | `{"type": "template", "template": ...}`, or `{"type": "decision", "questions": [...], "include": {...}}`. A question has `type` (`yes_no`, `choice` or `score`), `name` and `text`, plus `yes`/`no`, `options` or `levels`. `include` selects the inputs, the caller data and the context items for the request (default: all). |
| `result` | A template. `decline` passes the invocation to an agent turn. Without it, the result is the action output. |
| `writes` | Up to 10 service calls: `service`, `entity_id` and `data` (each value a template that gets `result`). |
| `cache` | `seconds` and `keys` (input entity IDs, or `data.<key>`). |
| `agreement` | A template that receives `rule_result` and `agent_result`. Backtests and trials use it. See "Backtests and trials". |
| `trial` | `enabled` (default false) and `rate` (default 0.2). See "Backtests and trials". |

### Checks

At create and update time (`rules/checks.py`, the `SPEC_CHECKS` entry for
`rule`):

- Each template is in the subset (`invalid_template`).
- A decision action needs `models.decision` (`no_decision_model`).
- A `history` item names entities in the read scope.
- An `action_response` service declares `"response": {"optional": false}`
  in HA (`not_response_only`), passes `check_service_call` with
  `return_response`, and needs no approval.
- A `store_file` item names an attached store and a safe path.
- Each write passes `check_service_call` with placeholder data values and
  needs no approval (`approval_required`). Rules cannot wait for approval.

When the rule runs:

- The write checks run again before any part of the rule runs.
- Context items and writes run through `ToolExecutor.without_approval()`.
  A call that needs approval is denied (`approval_unavailable`) and has an
  audit row. Writes go through `ha.call_service`, so they have an audit row
  and reach `ToolEnv.calls`, the own-write observer of the scheduler.
- The executor checks the rendered write data again.
- A cached context item gets its scope check again before use. The check
  does not reach HA or a store.

### Run

`RuleEngine.try_rules` tests the active rules in order. The first rule whose
match is true runs. A decline goes to the agent turn, not to the next rule.
A rule that fails records the error and the router starts an agent turn. A
decision call retries with backoff on a connection error, a rate limit or a
server error. If it still fails, the rule declines.

- Template rules need neither HA nor the internet. While HA restarts, the
  templates use the last state cache and the last registry. Writes fail
  closed (`states_unavailable`), and the rule records `rule_error`.
- The result cache and the context cache are in memory. They are empty
  after an app start.

### Store and versions

Migration `0700` holds `rules`, `rule_versions` and `rule_stats`.

- `self.create` writes version 1 in the state `draft`. `self.update`
  writes the next version, makes it current and sets `draft`.
  `self.set_enabled` sets `active` or `off`. Only active rules answer.
- `self.rollback_rule` makes an older version current. Its statistics
  continue. `self.reorder_rules` takes each rule ID of the agent once.
- `self.delete` marks the rule as deleted. Its versions and statistics stay
  for history.
- `rule_stats` counts `matched`, `answered`, `declined`, `cache_hits`,
  `errors` and `cost_pusd` for each version. `memory.rule_stats` reads
  them through `TurnHistory.rules`. `RuleStore.version_extras` adds the
  backtest and trial figures (task-14).

Every SQL statement for an agent's rules filters by the agent ID.

### Options

The `rules` section of `options.json` holds the keys in `RulesConfig`
(`src/hagent_app/rules/config.py`): decision retries, the render time limit
and the cache size.

## Backtests and trials

The `backtest` package lets an agent test a rule before and after it turns
the rule on (design sections 10.1 and 10.7). `BacktestComponent` owns three
parts:

- **Invocation records**: the inputs, the full input states, the context
  values and the decision answers of each invocation that a rule can answer.
- **Backtests**: a run of one rule version against recorded history. It
  writes nothing.
- **Trials**: a dry-run agent turn next to a rule that answered, on a
  sample of matches.

Terms:

- **Dry run**: a run in which tools check the scope and record changes, but
  do not make them (`ToolContext.dry_run`).
- **Agreement template**: the `agreement` template of a rule. It receives
  `rule_result` and `agent_result`. Without it, two results agree when they
  have the same JSON form.
- **Item key** and **request key**: the keys of a context value and of a
  decision answer in a record. See `backtest/records.py`.

### Wiring

`default_components` builds `BacktestComponent` before `RulesComponent`:

- `RulesComponent` gets `backtest.decision_source()`. It records each
  decision answer for its invocation (`current_invocation`).
- `BacktestComponent.start` replaces `RuleEngine.context` with
  `RecordingContextCollector`, sets `RuleStore.version_extras` (backtest and
  trial figures in the rule statistics) and `AgentStateStores.backtests`
  (the `self.backtest_rule` tool).
- The router uses `BacktestComponent` as its rule stage.
  `BacktestComponent.try_rules` calls `RulesComponent.try_rules` and writes
  the record also when no rule is active. A draft rule can then run a
  backtest on invocations from before it existed.

List it after `rules` and `turns` and before `router`. `rules/stage.py` has
no backtest code.

### Records and retention

Migration `0750` holds `invocation_records`, `rule_backtests`,
`rule_backtest_cases` and `rule_trials`. The component deletes records and
backtest cases older than `backtest.retention_days` (30) at start and every
`backtest.purge_hours` (6). It never keeps less than `backtest.max_days`.
The `invocations` and `turns` tables stay.

### Backtest

`Backtester.run(agent, rule_id, BacktestOptions(...))` runs one backtest.
`POST api/agents/{id}/rules/{rule_id}/backtests` and the tool
`self.backtest_rule` call it. `docs/api.md` ("Run a backtest") gives the
steps and the replay rules. Points for maintainers:

- `DryRunEvaluator` (`backtest/evaluate.py`) repeats the steps of
  `RuleEngine._answer` with a fixed clock, recorded input states and
  `HistoryContext`. A change to the live steps needs the same change here.
  `test_the_dry_run_matches_the_live_rule` compares the two.
- The evaluator uses `ToolExecutor.without_approval()` and a tool context
  with `dry_run` set. It calls the executor only for reads
  (`ha.get_statistics`, `knowledge.read`). For each write, it runs the
  `authorize` step of `ha.call_service` on the rendered data and the
  approval check, as the executor does for a live write. It records the
  write in the case and never sends it, so a backtest adds no audit row.
- It never calls `RuleStore.record` and never uses the live result cache.
- It never calls an `action_response` action again. It takes the response
  from `turn_tool_calls` of the turn, or skips the case.
- A history without a record (from before `0750`) takes the input values
  from `turns.input`.
- The runner runs in its own task and sets `current_invocation` to the
  backtest ID, so the cost of its decision requests is exact.
  `BudgetExceededError` stops it with the status `budget`.
- One backtest at a time runs for each rule (`backtest_running`).
- `utcnow()` reads the real time. Use `now()` in rules that backtests test.

### Trials

`TrialRunner` (`backtest/trials.py`) starts a trial when a rule with
`trial.enabled` answers and `BacktestComponent.sample()` is below
`trial.rate`:

- The trial runs `TurnsComponent.run_turn` with `TurnInput.dry_run`, in its
  own task, with the trial ID as `current_invocation`. The caller does not
  wait for it.
- It reads the agent and installation budgets first. A spent budget ends
  the trial with the status `budget`, and no turn runs.
- One trial at a time runs for each rule. `stop()` cancels running trials.
- A later backtest also compares with the result of a finished trial.

### Options

The `backtest` section of `options.json` holds the keys in
`BacktestConfig` (`src/hagent_app/backtest/config.py`).

## Schedules and subscriptions

The `scheduler` package runs the invocations that agents schedule for
themselves, and the review schedule of each definition (design sections 12
and 14). `SchedulerComponent` starts after the router. It submits each
invocation through `RouterComponent.submit`.

Terms:

- **Schedule**: a timer or a cron schedule that an agent creates with
  `self.create`. It invokes the agent with the source `schedule`, or
  `review` if the spec sets `review: true`.
- **Review schedule**: the `review.cron` of the definition. It invokes the
  agent with the source `review`.
- **Subscription**: a filter on HA state changes or events. A match invokes
  the agent with the source `event`.
- **Job**: one APScheduler entry: `item:<item ID>` for a schedule,
  `review:<agent ID>` for a review schedule.
- **Own write**: a state change that a service call of the subscriber
  caused.

### Schedule specs

```json
{"type": "timer", "delay_seconds": 900, "note": "check the boiler"}
{"type": "timer", "at": "2026-12-01T06:30:00+00:00", "review": true}
{"type": "cron", "cron": "30 6 * * 1-5", "data": {"room": "office"}}
```

- A timer takes exactly one of `delay_seconds` and `at`. `at` needs a UTC
  offset. A timer runs once. The app then deletes it.
- A cron schedule uses the standard 5-field form, in the time zone of the
  app. Day of week `0` and `7` are Sunday. A spec cannot set both the day
  of the month and the day of the week. The definition review cron can; it
  then fires only when both match.
- `note` and `data` go to the invocation in `data.schedule.note` and
  `data.data`. `data.schedule` also holds the item ID, the due time and
  `missed_count`.

### Library choice

APScheduler 3.11.3 runs the jobs. APScheduler 4 is still a pre-release
(4.0.0a6, April 2025) with an unstable interface. The SQLAlchemy job store
keeps the jobs in a separate SQLite file (`scheduler.job_store`). A
separate file keeps the job store out of the migrated schema and away from
the write lock of the app database. The table `scheduler_items` (migration
`0800`) is the source of truth. On each start, the component makes the job
store match it and the definitions.

APScheduler 3 calls the job store on the event-loop thread. The job store
is small, so these calls are short. Each job refers to
`scheduler.component:fire_job` by name, with the installation ID and the
job ID as arguments. APScheduler ships no type information, so mypy
ignores its imports.

### Limits

The definition sets the limits (`scheduling`):

- `max_schedules` counts schedules and subscriptions together. The count
  and the insert run in one transaction.
- `min_interval_seconds`: a timer must be at least this far ahead. The
  smallest gap between the fire times of a cron schedule must be at least
  this. At fire time, a schedule of the agent that comes less than this
  after its last submitted schedule run is skipped
  (`skipped_min_interval`). The definition review schedule does not count.
- `debounce_seconds` is a floor for the debounce of each subscription.
  The first match starts the debounce period. The app delivers all
  matches of the period in one invocation (`count`, and the last
  `max_changes_per_delivery` changes).

### Missed runs

`APScheduler` reads the wall clock directly. The component therefore
applies the missed-run policy itself, before APScheduler starts. It takes
a `clock` argument for tests. For each job that was due while the app was
stopped, it takes the last missed due time:

- Less than `missed_run_grace_seconds` (600) late: one invocation, with
  `missed_count` set to the number of missed due times.
- Otherwise: no invocation, and a `skipped_missed` row in
  `scheduler_runs`.

Then the job moves to its next future due time. `scheduler_runs` also
records each submitted run and its result.

### Subscriptions and own writes

- The component holds one HA event subscription for each event type in use.
  It never subscribes to `call_service`, also not for a row that bypassed
  the tools.
- `ToolsComponent.calls` is the `ServiceCallObserver` of the tool executor.
  `ha.call_service` calls `call_started` before it sends the call, and
  `call_finished` after it, also when the call fails. `OwnWriteTracker`
  then links each state change to the agent whose call caused it.
- A change counts as an own write only for the agent that made the call.
  By default, a subscription ignores own writes. With
  `include_own_writes: true`, it receives them.
- Before each delivery, the component reads the agent again. It drops the
  delivery if the agent is deleted or off. It runs the subscription spec
  check again against the current scope, so a narrowed scope stops the
  delivery (`denied`).
- Each delivered change has `origin` (`agent` or `external`) and `own`.
  The table `scheduler_events` records each matched event with its origin,
  the agent that caused it, and the outcome (`delivered`, `own_write`,
  `denied`, `dropped`).
- Before delivery, `redact` replaces approval action IDs
  (`HAGENT_ACCEPT_<id>`, `HAGENT_REJECT_<id>`) and approval tags in keys
  and values, at every depth.

### Agent state store

`SchedulerStateStore` is `ToolsComponent.agent_state`. It handles the kinds
`schedule` and `subscription`. `default_components()` sets
`scheduler.state_store.rules` to `RulesComponent.agent_state`, which
handles the kind `rule`.

The component follows `agents` change events. A change of the review
schedule moves the review job. A deleted agent loses its jobs, schedules
and subscriptions.

### Options

The `scheduler` section of `options.json` holds the keys in
`SchedulerConfig` (`src/hagent_app/scheduler/config.py`). Rows of
`scheduler_runs` and `scheduler_events` older than `retention_days` are
deleted on start and each hour.
## Inspect routes

`InspectComponent` (`inspection/`) serves the read-only routes of the
inspect area (design section 7.2). `docs/api.md`, section "Inspect",
describes them.

- The component reads the tables of other areas with plain SQL in
  `inspection/queries.py`. It never writes.
- It never reads the `rules` and `rule_versions` tables. The UI reads
  rules through the rule routes of `backtest/api.py`.
- Each payload passes `scheduler.redact.redact`, so that an approval
  action ID never reaches the UI.
- Permissions resolve the scope with `tools.scope.agent_scope` and the
  last registry snapshot. The entity list comes from the state cache.
- The component registers a result listener and an activity listener on
  the router. It publishes `invocations` / `finished` and
  `invocations` / `activity`. A queued invocation sends no event until it
  starts.

## The integration link

The `link` package serves the WebSocket for the HA integration (design
section 6). `docs/link.md` is the protocol. `docs/link-messages.json` holds
example messages. The app tests and the integration tests both load it, so
that the two sides cannot drift apart.

Rules:

- `LINK_PATH` (`/api/link/ws`) is the only path that the Ingress guard does
  not check. The upgrade request must carry `Authorization: Bearer
  <secret>`. Otherwise the app answers 401 and does not open the
  WebSocket. `test_link.py` proves both points.
- The app creates the secret on the first start in `<data dir>/link_secret`
  (mode 0600). `GET api/integration` shows it. That route is guarded like
  every other route.
- `hello` holds every live agent. The integration removes devices of agents
  that are not in it.
- `invoke` calls `RouterComponent.submit`. The link applies `wait`,
  `timeout` and `on_timeout`. For `on_timeout: cancel` it calls
  `router.cancel`.
- `router.add_result_listener` gives each final result to the link. The
  link sends it as a `result` message, with `delivered` true if a waiting
  command got it as response data.
- `router.add_activity_listener` tells the link when an invocation starts
  or ends. The link then sends the agent snapshot with the new status.
- `set_enabled` uses `AgentService`. The link is a management surface, not
  an agent tool.
- Inside an app container, the link posts its host, port and secret to the
  Supervisor (`POST /discovery`, service `hagent`). `hagent/config.yaml`
  lists `hagent` under `discovery`.

The `link` section of `options.json` holds the keys in `LinkConfig`
(`src/hagent_app/link/config.py`).

### Attachments

`InvocationRequest.attachments` and `TurnInput.attachments` hold
`Attachment` objects (`turns/models.py`). The request refuses types outside
`ATTACHMENT_TYPES` and more than `ATTACHMENT_MAX_BYTES` in total. The
runner gives images and PDF files to the model as `BinaryContent`, and text
files as text marked as data. The queue row keeps the attachments as
base64, so a resumed invocation still has them.

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
| 1000–1099 | Settings | task-20 |

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
`ConfigError`. The Supervisor writes only the keys of the option schema
in `hagent/config.yaml`. To let the user set a key on the Configuration
tab, add it to `options`/`schema` there and to
`hagent/translations/en.yaml`. `test_app_package.py` checks that each
schema key is a field of the section dataclass.

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
- Code reads the keys through a `KeySource`. In the app it is the
  `SettingsKeySource` of the settings area: the key from the UI, then
  `HAGENT_OPENAI_API_KEY` and `HAGENT_OPENROUTER_API_KEY`, then the
  `providers` section of `options.json`. A key goes only into the
  `Authorization` header of a provider request. Error texts pass through
  `redact`.
- With a `SettingsComponent`, the budgets, the price overrides, the
  decision models and `genai_prices_auto_update` come from the settings,
  with the options as defaults. A change takes effect without a restart.
- The daily budget of an agent is `budget.daily_usd` of its current
  definition. The meter reads it through `Meter.budget_lookup` on each
  reservation, so an edit applies to the next call. When the value is
  `null`, or the agent does not exist, the meter uses the
  `agent_budgets_usd` option for that agent, then the default agent budget
  (settings, else the option).
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
