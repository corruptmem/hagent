
<!-- BACKLOG.MD GUIDELINES START -->
<!-- backlog.md-instructions-version: 1.49.3 -->
<CRITICAL_INSTRUCTION>

## Backlog.md Workflow

This project uses Backlog.md for task and project management.

**For every user request in this project, run `backlog instructions overview` before answering or taking action.**

Use the overview to decide whether to search, read, create, or update Backlog tasks.

Before task lifecycle actions, read the matching detailed guide:
- `backlog instructions task-creation` before creating or splitting tasks
- `backlog instructions task-execution` before planning, changing status or assignee, adding a plan or implementation notes, or implementing task work
- `backlog instructions task-finalization` before checking acceptance criteria, writing final summaries, or moving tasks to terminal statuses

Use `backlog <command> --help` before running unfamiliar commands. Help shows options, fields, and examples.

Do not edit Backlog task, draft, document, decision, or milestone markdown files directly. Use the `backlog` CLI so metadata, relationships, and history stay consistent.

</CRITICAL_INSTRUCTION>
<!-- BACKLOG.MD GUIDELINES END -->

# Agent notes for hagent

hagent is an agent manager for Home Assistant (HA). Read `docs/design.md`
before you change the architecture. Do not add features that serve only one
use case. Agents build use-case logic from the generic primitives.

## Layout

- `app/`: the hagent app (agent runtime and management UI back end).
- `integration/custom_components/hagent/`: the HA integration. HA Core loads
  it with its own interpreter. It must not import the app.
- `integration/pyproject.toml`: a separate uv project for the integration
  tests, because HA Core pins conflict with the app. `tests/` runs with
  pytest-homeassistant-custom-component (HA Core 2026.10.4). `tests_ha2611/`
  and mypy run against HA Core 2026.11.1.
- `docs/link.md`: the link protocol between the app and the integration.

All Python code targets Python 3.14, the version that HA Core requires. The
app uses the same version, so that the app and the integration can share code
and tooling.
- `frontend/`: the management UI (React, TanStack, Vite). Read
  `frontend/README.md` before you change it.
- `backlog/`: work tracking. Use the `backlog` CLI, not direct edits.
- `docs/design.md`: the design.
- `docs/api.md`: the management API contract. Update it when you add or
  change a route.

## Tools

mise installs the tools (`mise install`). uv manages Python and
dependencies. Run `mise run setup` once to sync dependencies and install the
pre-commit hooks.

## Checks

Run `mise run check` before you commit. It runs:

- `ruff check` and `ruff format --check`: lint, static analysis (including
  security rules) and formatting.
- `mypy` in strict mode: type checking.
- `pytest`: tests.
- the integration type check and tests (`typecheck-integration`,
  `test-integration`).
- `npm run check` in `frontend/`: eslint, prettier, tsc and vitest.

pre-commit runs ruff and mypy on each commit, and eslint, prettier and tsc
when a commit changes `frontend/`.

## Rules

- Safety: the app has administrator access to HA. Every tool call must pass
  the runtime scope check, for HA and for knowledge stores. Each new tool
  needs a test that proves an out-of-scope call never reaches HA or the
  store.
- Never commit secrets or `.env` files.
- Tests must not need a live HA instance or a provider API.
- The repository is public. Do not commit personal names, entity IDs or
  details of a real home.
- Do not link to local paths outside the repository.
- Write documents in ASD-STE100 style: short sentences, active voice, and a
  definition for each technical term at first use.
