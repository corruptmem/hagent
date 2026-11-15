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
  Python test keeps that fixture equal to `GET api/agents/defaults`.
- The knowledge editor (`KnowledgeEditor`) takes an optional `stores`
  list. Without it, the user types a store ID. When the stores API exists
  (task-11), pass the list from `Editor` in `src/screens/AgentEditor.tsx`.

## Packaging (task-21)

Git does not hold the build. The app image must build it:

1. Run `npm ci` and `npm run build` in `frontend/`, with the Node version
   from `mise.toml`. A multi-stage Docker build can do this in a Node
   stage.
2. Copy `frontend/dist/` into the app image, for example to
   `/opt/hagent/ui`.
3. Set the `ingress.ui_dir` option to that directory, or give it that
   default in the app option schema.

The app image does not need Node at run time.
