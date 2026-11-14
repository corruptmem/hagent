# hagent

hagent is an agent that manages a home through Home Assistant (HA). It sends
your request to a Claude large language model (LLM). An LLM is a model that
reads and writes text. The LLM uses tools to
read HA entities and, if you allow it, to call HA services.

hagent is read-only by default. It changes devices only when you set
`HAGENT_ALLOW_ACTIONS=true`. It then asks you to approve each service call.

## Requirements

- Python 3.12 or newer, and [uv](https://docs.astral.sh/uv/).
- An HA instance and a long-lived access token. To create a token, open your
  HA profile page, go to the Security tab, and select "Create token".
- An Anthropic application programming interface (API) key.

## Set up

```bash
uv sync
cp .env.example .env   # then fill in the values
set -a; source .env; set +a
```

| Variable | Required | Meaning |
|---|---|---|
| `HA_URL` | Yes | Base URL of HA, for example `http://homeassistant.local:8123`. |
| `HA_TOKEN` | Yes | HA long-lived access token. |
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key. The Anthropic software development kit (SDK) reads it. |
| `HAGENT_MODEL` | No | Claude model ID. The default is `claude-sonnet-5-5`. |
| `HAGENT_ALLOW_ACTIONS` | No | Set to `true` to let hagent call services after approval. |

## Use

```bash
uv run hagent "Which lights are on?"   # one question
uv run hagent                          # interactive chat
```

## Develop

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
```

The tests use a fake HA and a fake LLM. They need no network access.

## Documents

- [Architecture decisions](contributing/decisions/README.md)
- [Project log](docs/timeline.md)
