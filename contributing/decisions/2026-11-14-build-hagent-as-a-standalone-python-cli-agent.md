# Build hagent as a standalone Python CLI agent

## Context and Problem Statement

hagent is an agent that manages a home through Home Assistant (HA). HA is an
open-source home automation platform. It exposes devices as entities and
device actions as services.

An agent here is a program that sends a conversation to a large language
model (LLM). The LLM asks for tool calls, and the program runs them. The tools
read and change HA.

The repository started empty. The first version needs a shape that runs
locally, tests without a live HA, and does not change physical devices by
accident. Some devices are unsafe to change without a person's approval, for
example locks, alarms and heating.

## Decision

Build hagent as a standalone Python 3.12 package with a command-line
interface (CLI).

- Use the Anthropic Python software development kit (SDK) for the LLM. Run the tool-use loop in hagent
  code, not in a framework. The default model is `claude-sonnet-5-5`. The
  `HAGENT_MODEL` variable overrides it.
- Use the HA REST API through `httpx`. An application programming interface
  (API) is the set of requests that a program accepts. REST is a style of API
  that uses plain HTTP requests. Authenticate with a long-lived access
  token. A long-lived access token is a bearer token that an HA user creates
  on their profile page.
- Read all settings from environment variables. Keep secrets out of the
  repository.
- Give the LLM read tools by default: `list_entities`, `get_state` and
  `list_services`.
- Give the LLM the `call_service` tool only when `HAGENT_ALLOW_ACTIONS` is
  true. Each service call then needs approval from the user. The CLI asks for
  approval in the terminal.
- Inject the LLM client and the HTTP transport. Tests replace both with
  fakes.

Non-goals for this version: a long-running service, voice input, memory
across sessions, automations, and an HA add-on package.

Accepted costs:

- The CLI runs only when a person starts it. It cannot react to HA events.
- The REST API has no event subscription. Each tool call fetches state again.
- `list_entities` returns all entities of a domain. Large installations can
  produce large tool results.
- A manual loop needs more code than a framework. In exchange, the code is
  small, and tests can control each step.

## Alternatives Considered

- HA conversation agent integration: a custom integration that HA loads. HA
  Assist and voice pipelines can then use it. It runs inside the HA process
  and uses HA release cycles. It is harder to develop and test outside HA.
  Rejected for the first version. It stays an option for a later interface.
- HA add-on: a container that the HA Supervisor runs. It gives a stable host
  and a token from the Supervisor. It needs HA OS or a Supervised install, and
  it adds packaging work before the agent is useful. Rejected for now.
- HA WebSocket API: it supports event subscriptions and registry data, for
  example areas and devices. It needs a persistent connection and more code.
  Deferred until hagent needs events or area data.
- HA Model Context Protocol (MCP) server: MCP is a protocol that exposes tools
  to an LLM client. An existing HA MCP server gives many tools quickly. hagent
  then has less control over the safety of each tool. Rejected for the core.
  It can be an option for a later version.
- Strands on Amazon Bedrock, as the Theo project uses: it needs AWS
  credentials and an agent framework for a single-user local tool. Rejected.

## More Information

Open decisions. These need a decision from the owner before the work starts:

- Where hagent runs for a long time: a home server process, an HA add-on, or
  inside HA as a conversation agent.
- How other agents call hagent, for example the owner's Tars assistant. The
  options are MCP, the Agent2Agent (A2A) protocol, or an HTTP API.
- Which domains and services stay blocked even with approval, for example
  `lock` and `alarm_control_panel`.
- When to move from REST to the WebSocket API.

Revisit this record when hagent must react to HA events, run without a person
at the terminal, or accept requests from another agent.
