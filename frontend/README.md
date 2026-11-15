# hagent management UI

This package is the management UI of hagent (design section 7.2). It is a
React single-page application (SPA) with TanStack Router, TanStack Query
and TanStack Table, built with Vite. `docs/api.md` is the contract with the
app.

## Terms

- **SPA**: a web application that loads one HTML page and changes the
  screen in the browser.
- **Base path**: the URL prefix of the UI. Under Ingress it is
  `/api/hassio_ingress/<token>`. The app sends it in the `X-Ingress-Path`
  header and adds it to `index.html` as a `<base>` element.

## Commands

Run the commands in `frontend/`. `mise run setup` installs the
dependencies with `npm ci`.

| Command          | Effect                                                               |
| ---------------- | -------------------------------------------------------------------- |
| `npm run dev`    | Starts the Vite development server. It forwards `/api` to the app.   |
| `npm run build`  | Checks the types and writes the build to `dist/`.                    |
| `npm run check`  | Runs eslint, prettier, tsc and vitest. `mise run check` runs it too. |
| `npm run format` | Applies the prettier format.                                         |

## Development with a local app

1. Allow local connections in the app options. Write this file to
   `$HAGENT_DATA_DIR/options.json`:

   ```json
   { "ingress": { "allowed_addresses": ["127.0.0.1", "::1"] } }
   ```

   The default allows only the Supervisor address, `172.30.32.2`.

2. Start the app: `uv run python -m hagent_app`. See `app/README.md` for
   the environment variables.
3. Start the UI: `npm run dev`. The development server forwards `/api` and
   the event WebSocket to `http://127.0.0.1:8099`. Set `HAGENT_APP_URL` to
   use another address.

To test the build with the app, run `npm run build`. The app serves
`frontend/dist` when the `ingress.ui_dir` option is empty.

## Rules

- Use relative URLs only. `src/base.ts` resolves each API path against the
  `<base>` element. Never write a path that starts with `/`.
- Build with `base: "./"` (see `vite.config.ts`), so that each asset URL in
  `index.html` is relative.
- Load data with TanStack Query. The change-event WebSocket
  (`src/events.ts`) invalidates the queries.
- Each definition field has a control with a `data-path` attribute. A test
  checks this for each field of `src/test/fixtures/defaults.json`. A
  Python test keeps that fixture equal to `GET api/agents/defaults`,
  except `budget.daily_usd`, which the app takes from the settings.
- The knowledge editor (`KnowledgeEditor`) takes a `stores` list.
  `Editor` in `src/screens/AgentEditor.tsx` loads it from
  `api/knowledge/stores`. If that request fails, the editor asks for a
  store ID instead.
- Never show an API key. The settings API returns only a hint. The key
  field is a password field, and it is empty again after a save.
- An item that arrives in parts (more than 20000 characters) is
  read-only. A save sends the whole file, so a save of one part would lose
  the other parts.
- Encode each part of an item path with `itemPath` in `src/api/client.ts`.
  Keep the `/` between parts.

## Screens

| Path             | Screen          | Content                                                                                                                                                                                           |
| ---------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agents`         | `AgentsList`    | The agents. Create, copy, turn on or off, delete.                                                                                                                                                 |
| `agents/<id>`    | `AgentShell`    | The definition, the versions and the test panel of one agent.                                                                                                                                     |
| `knowledge`      | `KnowledgeList` | The stores, their size and the agents that attach each store. Create a store.                                                                                                                     |
| `knowledge/<id>` | `StoreView`     | One store: its settings, its agents and its items. Upload, replace and delete documents. Create, edit and delete files. See the history of a path, also of a deleted file, and restore a version. |
| `settings`       | `Settings`      | API keys, the embedding model, budgets, approvers, price overrides, the genai-prices auto-update and decision models.                                                                             |

## Packaging

Git does not hold the build. The app image builds it (`hagent/Dockerfile`):

1. A Node stage runs `npm ci` and `npm run build` in `frontend/`.
2. The app stage copies `frontend/dist/` to `/opt/hagent/frontend/dist`.
   `/opt/hagent` keeps the layout of the repository, so the default
   `ingress.ui_dir` (empty: `frontend/dist` next to `app/`) finds it.

The app image does not need Node at run time.
