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
| `src/hagent_app/own_writes.py` | `OwnWriteTracker`. It links state changes to the service calls that caused them (task-2). |
| `tests/` | The tests. |
| `tests/helpers/fake_ha.py` | `FakeHA`, a fake HA WebSocket server. Use it in every test that needs HA. |
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
tool executor (task-8), which checks the scope first.

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
