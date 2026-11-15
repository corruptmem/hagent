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
| `src/hagent_app/turns/` | The agent turn, the turn history and the running summaries (task-10). See "How to run an agent turn" below. |
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
  `KnowledgeStore.search` receives the agent ID from the executor. A search
  from the management API counts towards the installation. A query that is
  in the query cache costs nothing. A model change re-indexes every passage. The embedder deletes vectors of
  other models and vectors whose text no longer exists.
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
  `turns.index_max_entries` as the index size (50 by default). It gives
  the store list, the name-sorted index and the injected main files. The
  text holds no time or other value that changes between turns. The
  builder also uses it for the main file requests (design section 13.1).
  If the call fails, the builder reads the stores through the executor.
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
`HistoryIndex` (task-11) indexes the turns and serves `memory.search`:
`default_components()` sets `turns.history.index`. Without an index,
search uses a plain text match. Dry runs do not go into the index, the search or the summaries.

### Options

The `turns` section of `options.json` holds the keys in `TurnsConfig`
(`src/hagent_app/turns/config.py`).

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
