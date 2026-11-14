# Project log

## 2026-11-14: Initial scaffold

Created the repository with a one-line README.

Built the first version of hagent as a Python 3.12 package with a CLI.

- Added an HA REST client. It reads states and services, and calls services.
- Added three read tools for the LLM: `list_entities`, `get_state` and
  `list_services`.
- Added a `call_service` tool. It is off by default. When it is on, each call
  needs approval in the terminal.
- Added a tool-use loop on the Anthropic Messages API. The default model is
  `claude-sonnet-5-5`.
- Added 15 tests with a fake HA and a fake LLM. Added ruff and strict mypy.

Recorded the architecture in an ADR. An architecture decision record (ADR) is
a short document that states one decision and its reasons.

Found that mypy rejected the SDK `Messages` object against a `create(**kwargs)`
protocol. Changed the protocol member to a read-only callable property.

Not yet tested against a live HA instance or the Anthropic API.

Next:

- Run hagent against the home HA instance.
- Decide where hagent runs long term, and how Tars calls it.
- Decide which domains stay blocked even with approval.
