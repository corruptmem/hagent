# Agent notes for hagent

## Layout

- `src/hagent/config.py`: settings from environment variables.
- `src/hagent/ha.py`: Home Assistant (HA) REST client.
- `src/hagent/tools.py`: tool definitions for the LLM, and the code that runs them.
- `src/hagent/agent.py`: tool-use loop against the Anthropic Messages API.
- `src/hagent/cli.py`: `hagent` command.
- `tests/`: tests with a fake HA (`httpx.MockTransport`) and a scripted fake LLM.

## Checks

Run all three before you commit:

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
```

## Rules

- Safety: hagent can change physical devices. Keep read-only the default. Any
  new tool that changes HA state must go through the approval callback in
  `Toolbox`. Add a test that proves a rejected call sends no request to HA.
- Never commit tokens or `.env`. Use `.env.example` for new variables.
- Tests must not need a live HA or the Anthropic API.
- Record architecture decisions in `contributing/decisions/`. Read the
  existing records before you change the architecture.
- Add an entry to `docs/timeline.md` for each change that matters to the
  project history.
- Write documents in ASD-STE100 style. Write short sentences. Use active
  voice. Define a technical term the first time a document uses it.
- Do not link to local paths outside the repository.
