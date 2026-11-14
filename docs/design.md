# hagent design

Status: draft for review. No code exists yet.

## 1. Purpose

hagent is an agent manager for Home Assistant (HA). HA is an open-source home
automation platform. It models devices as entities, and device actions as
services.

An agent is a configured program that uses a large language model (LLM) to
do work. An LLM is a model that reads and writes text. In hagent, the user
defines each agent: its instructions, models, tools and permissions. hagent
runs the agents and keeps their memory and history.

HA automations call hagent agents. An automation is an HA rule of the form
"when a trigger occurs, if conditions are true, run actions." Agents can also
start themselves on a schedule or on an HA event. A person can talk to an
agent through Assist, if the definition allows it. Assist is the HA voice and
chat assistant.

hagent supplies general primitives. It does not contain logic for one use
case. Each agent builds its own behaviour from the primitives.

## 2. Problem

HA has AI features, but each call starts from zero:

- The `ai_task.generate_data` action returns structured data. It does not take
  a previous conversation as input.
- The `conversation.process` action continues a conversation, but HA deletes
  the session after 5 minutes without activity.

An automation that uses AI therefore must rebuild all context on every run.
It must also contain all its own logic for safety, caching and evaluation.
For example, the owner has heating automations that query days of history
and call a model every 5 to 15 minutes. Each run pays for the full context
again. Nothing remembers past decisions, and nothing improves the logic.

hagent gives each agent persistent memory and history, a tool system with
permissions, and the ability to replace repeated model calls with cheaper
rules that the agent writes itself.

## 3. Terms

| Term | Meaning |
|---|---|
| App | A container that the HA Supervisor runs next to HA Core. HA called apps "add-ons" before release 2026.2. |
| Integration | Python code that HA Core loads. It adds entities, actions and configuration screens to HA. |
| Agent definition | The configuration of one agent, owned by the user. |
| Agent state | The data that an agent creates and changes itself: rules, schedules, notes and history. |
| Invocation | One call to an agent. |
| Agent turn | An invocation that the LLM handles, with instructions, memory, tools and history. |
| Tool | A function that an agent can call during an agent turn. |
| Scope | The set of HA data and actions that a tool may use for one agent. |
| Rule | A fast path that an agent writes. A rule answers an invocation without an agent turn. |
| Decision model | A model that returns typed answers with probabilities, not text. Examples: the OpenAI Decisions API, and Jev on OpenRouter. |

## 4. Requirements

- R1. Automations call an agent explicitly. This is the primary trigger.
- R2. An agent can schedule its own invocations, from timers or from HA
  events.
- R3. The definition can attach entities or devices as inputs and as outputs.
  An output attachment lets the agent set that entity.
- R4. An agent can return text or structured output to the caller instead.
- R5. Each agent has persistent memory, notes and history.
- R6. An agent can write rules. A rule answers an invocation by a template
  calculation or by one decision model call, without an agent turn.
- R7. An agent wakes up on a schedule to review its rules and change them.
- R8. The definition lists the tools of the agent: self-modification, memory
  and HA access.
- R9. Each HA tool has a scope: read-only, specific entities or devices, or
  full administration, including changes to automations.
- R10. The definition can expose the agent to Assist. This is optional and
  off by default.
- R11. hagent supports OpenAI and OpenRouter.
- R12. hagent runs as an HA app.
- R13. Cost is a primary design goal.
- R14. The design is generic. No feature exists for one use case only.

## 5. Prior art

- **Jarvis-AIO** is a community integration. It learns routines, and acts
  alone after the user accepts the same suggestion 3 times. It sends events
  through tiers: templates, a cache, a classifier model, then a large model.
  It is one fixed assistant. hagent instead hosts agents that the user
  defines. hagent takes the tier idea as the rule primitive.
- **HA AI Task and conversation agents** are one-shot or short-lived.
- **Memory integrations for Assist** add notes to chat agents. They do not
  add rules, schedules or background invocation.

## 6. Architecture

hagent has two parts: an app and an integration.

```
+--------------------------- Home Assistant host ----------------------------+
|                                                                            |
|  HA Core                                  hagent app (container)           |
|  +--------------------------------+       +------------------------------+ |
|  | hagent integration             | link  | Agent runtime                | |
|  |  - one device per agent        |<----->|  - agent definitions         | |
|  |  - hagent.invoke action        |       |  - router + rule engine      | |
|  |  - per-agent entities          |       |  - tool executor + scopes    | |
|  |  - Assist agents (optional)    |       |  - scheduler                 | |
|  +--------------------------------+       |  - memory, history (SQLite)  | |
|  | Automations, entities, events  |<------|  - provider adapters         | |
|  +--------------------------------+  HA   |  - management UI (Ingress)   | |
|                                      API  +--------------+---------------+ |
+----------------------------------------------------------|-----------------+
                                                           | HTTPS
                                                OpenAI / OpenRouter
```

**The integration** is thin. It holds no agent configuration:

- It receives the list of agents from the app, and keeps it current. When
  the user creates, changes or deletes an agent in the app, the integration
  adds, updates or removes the matching device.
- It creates one HA device for each agent, with these entities:
  - a status sensor (idle, running, budget spent, error);
  - a last-result sensor;
  - a cost-today sensor;
  - an enabled switch;
  - an AI Task entity (section 9.3);
  - a conversation entity, only if the agent allows Assist.
- It provides the `hagent.invoke` action (section 9). The action targets the
  agent device.
- It forwards invocations to the app and returns the results.

**The app** does all agent work:

- It runs outside the HA Core event loop, so a slow model call cannot delay
  HA. It continues to run when HA Core restarts.
- It connects to HA Core through the Supervisor proxy, with the
  `SUPERVISOR_TOKEN` that the Supervisor supplies. It uses the HA WebSocket
  API for state, events and service calls.
- It keeps agent state in SQLite in the app data directory. HA backups
  include this directory.
- It stores agent definitions, and serves a management UI through Ingress.
  Ingress shows an app web page in the HA sidebar, behind HA login. Only HA
  administrators can open it. Section 7.1 describes the UI.

**The link** is a WebSocket connection on the internal Supervisor network.
The user enters the app host name in the integration once. The app
authenticates the integration with a shared secret that the app creates on
first start.

### 6.1 Ingress rules for the management UI

HA does not prescribe a front-end framework. The Ingress proxy sets these
rules:

- The browser reaches the UI under `/api/hassio_ingress/<token>/`. The UI must
  use relative URLs. HA sends the base path in the `X-Ingress-Path` header.
- The app listens on port 8099, or on the port in `ingress_port`.
- The app accepts connections only from the Supervisor address,
  `172.30.32.2`.
- The UI does not ask for a login. HA handles authentication. HA can send
  user identity headers, but it does not guarantee them. The app therefore
  relies on `panel_admin` to limit access to administrators.
- The proxy supports HTTP/1.x, streaming responses and WebSockets.

### 6.2 Installation

For the first version, the user installs both parts by
hand. The user adds the app from a local app folder, and copies the
integration into the HA `custom_components` folder.

## 7. Agent definition and agent state

The definition and the state are separate. This separation makes scope
enforcement possible. The app stores both, in separate tables.

**Agent definition** (owned by the user, edited only in the management UI):

| Field | Content |
|---|---|
| Name and instructions | What the agent is for, in plain text. |
| Models | A chat model, and an optional decision model. Each has a provider. |
| Inputs | Entities or devices whose current values go into every invocation. |
| Outputs | Entities that the agent may set. |
| Result type | `none`, `text`, or `structured` with a JSON schema. |
| Tools and scopes | Section 8. |
| Approval | Tools or scopes that need the user's approval for each call. |
| Assist | Off by default. |
| Budget | A daily cost limit, and limits for self-scheduled invocations. |
| Review schedule | When the agent receives a review invocation. The default is daily. |

**Agent state** (owned by the agent, stored in the app):

- Rules.
- Schedules and event subscriptions.
- Notes.
- History of invocations, tool calls, results and costs.

An agent can read its definition, but no tool writes to it. Only the user
changes a definition, through the management UI. An agent therefore cannot
widen its own scope, outputs or budget.

### 7.1 Management UI

The management UI is a web application in the app. It has two jobs: define
agents, and inspect what they do.

**Define**

- Create, edit, copy, turn off and delete agents.
- Pick inputs, outputs and HA scopes from the live list of HA entities,
  devices, areas and labels.
- Turn tool groups on and off, and mark tools for approval.
- Choose providers and models, and enter API keys.
- Set the budget, the scheduling limits and the review schedule.
- Run a test invocation with sample data, without changes to HA.

**Inspect**

- Invocations: a list with filters by agent, source, path and result. Each
  entry opens a full trace: inputs, the rule or the model messages, every
  tool call with its scope decision, the result and the cost.
- Memory: the notes, with their change history.
- History: search across past invocations.
- Rules: each rule, its templates, its statistics and its trial agreement.
  The user can turn a rule off or delete it.
- Schedules and subscriptions: what is active, and when each one last ran.
- Permissions: the effective scope of each tool, and a log of denied calls.
- Changes to HA: the audit log, with the previous version of each `admin`
  change.
- Cost: by agent, by day and by path.
- Approvals: pending approval requests, as an alternative to the HA
  notification.

## 8. Tools and scopes

The definition turns on tool groups. hagent supplies three groups.

**`self`** — change agent state:

- Create, change, turn on, turn off and delete rules.
- Set, list and cancel timers and cron schedules.
- Subscribe to and unsubscribe from HA events and state changes, with a
  filter.

**`memory`** — notes and history:

- Read and replace notes.
- Search history.
- Read rule statistics (section 10.4).

**`ha`** — Home Assistant, at one of four levels:

| Level | Permitted |
|---|---|
| `none` | Nothing. The agent receives only its inputs. |
| `read` | Read states, history and statistics. The scope limits this to listed entities, devices, areas, labels or domains, or allows all. |
| `control` | `read`, plus service calls on permitted targets. The scope can also list permitted services. |
| `admin` | `control`, plus changes to HA configuration: automations, scripts, helpers, scenes and dashboards. |

An output attachment is a short form of `control` for one entity.

**Enforcement.** Through the Supervisor proxy, the app has administrator
access to HA. HA tokens have no fine-grained scopes. The runtime therefore
checks every tool call against the scope before it sends anything to HA. The
runtime, not the instructions, enforces the scope. Tests must show that a
call outside the scope never reaches HA.

**Approval.** For a tool or scope that the definition marks for approval, the
runtime sends an HA notification with accept and reject actions. The call
waits for the answer, up to a time limit.

**Audit.** The runtime records every call that changes HA. For `admin`
changes, it also saves the previous version of the changed item.

**Extension.** The tool system accepts more tool groups later, for example
web search or notifications. Each group declares its own scope format.

## 9. Invocation

### 9.1 Sources

An invocation comes from one of six sources:

1. An automation calls `hagent.invoke`.
2. An automation or script calls an HA AI Task action on the agent's AI Task
   entity (section 9.3).
3. A timer or cron schedule that the agent set.
4. An HA event or state change that the agent subscribed to.
5. The review schedule in the definition.
6. A person, through Assist, if the definition allows it.

Every invocation carries its source, the current input values and optional
data from the caller.

### 9.2 The `hagent.invoke` action

| Field | Meaning |
|---|---|
| `agent` | The agent to call. |
| `data` | Optional context, as a mapping. |
| `wait` | `true` (default): return the result as response data. `false`: return at once, and deliver the result later. |
| `timeout` | Maximum wait in seconds. The default is 30. |

When `wait` is true, the action returns response data:

```yaml
result: ...          # text or structured result, per the result type
path: rule           # rule or agent_turn
rule_id: r12         # if a rule answered
cost_usd: 0.0000     # cost of this invocation
```

When `wait` is false, or the timeout expires, hagent finishes in the
background. It then fires an `hagent_result` event and updates the
last-result entity of the agent.

If the app is not reachable, the action fails with an error. The automation
can catch it with `continue_on_error`. A connectivity entity shows the state
of the link.

### 9.3 The AI Task entity

AI Task is an HA entity type. Automations call it with standard HA actions,
for example `ai_task.generate_data`. Each agent device has one AI Task entity.
Any HA feature that accepts an AI Task entity can therefore use a hagent
agent, with no hagent-specific action.

The entity supports these AI Task features:

| HA feature | hagent behaviour |
|---|---|
| `GENERATE_DATA` | The task becomes an invocation with the source `ai_task`. The task name, instructions and attachments go into the caller data. If the task has a structure, that structure replaces the agent's result type for this invocation. The runtime returns the result as the task data. |
| `SUPPORT_ATTACHMENTS` | The runtime passes attachments to the model, if the chosen model accepts them. |
| `EVALUATE` | The task supplies questions and a state, and expects answers. The runtime handles it as an invocation with the source `ai_task_evaluate`. Rules can answer it, and a decision rule fits this task type directly. |

hagent does not support `GENERATE_IMAGE` in the first version.

Rules can match on the task name. An automation that sends the same task
every few minutes can therefore receive a rule answer at low cost.

HA gives each task a new chat log, and the AI Task action has no input to
continue an earlier conversation. This does not limit hagent: the agent
keeps its own notes and history across all invocations.

### 9.4 The router

The router handles each invocation in this sequence:

1. It collects the input values.
2. It tests the agent's rules in order. The first rule that matches answers
   the invocation (section 10).
3. If no rule matches, or the rule declines to answer, the runtime starts an
   agent turn.
4. The runtime records the invocation, the path, the result and the cost.

Review and Assist invocations always start an agent turn.

### 9.5 Agent turn

An agent turn sends the model:

- The instructions from the definition.
- The notes.
- A short description of the agent's rules, schedules and scopes.
- The invocation: source, inputs and caller data.
- Recent history for this source, within a size limit.

The model then calls tools in a loop until it returns a result. The runtime
checks each tool call against the scope, and records it.

## 10. Rules

A rule is a fast path that the agent writes with the `self` tools. Rules let
an agent answer repeated invocations at low or zero cost.

### 10.1 Rule structure

| Part | Content |
|---|---|
| Match | An HA template that returns true when the rule applies. It can test the source, the inputs and the caller data. |
| Action | `template` or `decision` (below). |
| Result | An HA template that builds the final result from the action output. It can also return "decline", which passes the invocation to an agent turn. |
| Writes | Optional output entities to set from the result. They must be in scope. |
| Trial | Optional. When on, the runtime also runs an agent turn on a sample of matches, and records whether the answers agree. The default sample rate is 1 in 5. Each rule can set its own rate. |

### 10.2 Template action

The action is an HA template. The app sends it to HA Core to render. HA
renders templates in a sandbox, so a rule cannot run arbitrary code.

Example: "Is anyone home?" computed from presence entities.

### 10.3 Decision action

The action asks the decision model in the definition one question:

- The question text, written by the agent.
- A list of choices: yes/no, categories or numbered options.
- The values to include, taken from the inputs and caller data.

The decision model returns a probability for each choice. The result
template turns the probabilities into the result. For example, it can take
the most likely choice, compute a weighted mean, or decline when no choice
is likely enough. The agent writes this logic. hagent does not build it in.

The OpenAI Decisions API and the OpenRouter decisions endpoint both return
typed answers with probabilities. hagent puts both behind one adapter. The
OpenRouter decisions endpoint is in alpha, so the adapter must expect
changes.

### 10.4 Rule statistics

The runtime records, for each rule:

- How often it matched, answered and declined.
- Errors.
- Cost.
- Trial agreement, if trial is on.

Agents read these statistics with the `memory` tools. hagent does not
promote, demote or delete rules by itself. The agent does this, usually in a
review.

## 11. Outputs and results

- **Output entities.** The agent sets an output entity with a tool call in an
  agent turn, or through the writes of a rule. The runtime checks the scope.
  hagent adds no limits beyond the scope. An agent that needs limits, for
  example a maximum change per step, writes them into its rules and notes. The
  user can also enforce limits in HA.
- **Text and structured results.** The agent returns the result to the
  caller. The runtime validates a structured result against the JSON schema.
  If it fails, the runtime asks the model once to correct it, then reports an
  error.

## 12. Self-scheduling

An agent with the `self` tools can create timers, cron schedules and event
subscriptions. Each one creates an invocation with its own source name, and
the router handles it like any other invocation. Rules can answer scheduled
invocations too.

The definition sets limits:

- The minimum interval between scheduled invocations. The default is 60
  seconds.
- The maximum number of schedules and subscriptions. The default is 20.
- A debounce period for each subscription, so that a sensor that changes
  often cannot cause many invocations.

The scheduler keeps schedules in SQLite and restores them when the app
starts.

## 13. Memory and history

- **Notes**: a text document that the agent edits. The runtime sends it with
  every agent turn. The definition limits its size, 4,000 characters by
  default.
- **History**: a record of each invocation, its inputs, path, tool calls,
  result and cost. The agent searches it with the `memory` tools.
- **Retention**: a daily task summarises old history and deletes raw records
  after a retention period, 30 days by default.

## 14. Review

A review is an invocation with the source `review`. The definition sets the
schedule, daily by default. An agent can also schedule extra reviews with the
`self` tools.

hagent does not define what a review does. The instructions and the notes of
the agent define it. A typical review reads rule statistics and recent
history, then changes rules and notes.

## 15. Assist exposure

If the definition allows it, the integration adds a conversation entity to
the agent device. HA then lists the agent as an Assist conversation agent. A person can then select the agent in an Assist
pipeline, or talk to it in the HA chat window.

A conversation is a series of agent turns with the same notes, history,
tools and scopes. The app keeps the conversation history, so it does not
expire after 5 minutes. Rules do not answer Assist invocations.

## 16. Providers

- **OpenAI**: chat models through the Responses API, and the Decisions API.
- **OpenRouter**: chat models through the OpenAI-compatible API, and the
  decisions endpoint, which provides Jev.

Each provider has one adapter. The adapter reports token use and cost for
each call. The user enters API keys in the management UI. The app never
sends keys to HA and never writes them to logs.

## 17. Cost controls

- Rules answer repeated invocations without an agent turn.
- Decision actions bill input tokens only.
- Notes and history summaries replace large context rebuilds.
- Each agent has a daily budget. The default is USD 0.50. When the budget is
  spent, agent turns stop and rules continue. The status entity shows the
  reason.
- Limits on self-scheduled invocations prevent runaway cost.
- Cost entities in HA show the cost per agent per day.

## 18. Failure handling

| Failure | Behaviour |
|---|---|
| The app stops. | `hagent.invoke` fails with an error. The connectivity entity turns off. |
| HA Core restarts. | The app reconnects and restores subscriptions. |
| A provider fails. | The runtime retries with backoff. A rule declines, and the runtime then tries an agent turn. If that also fails, the invocation returns an error. |
| A rule fails. | The runtime records the error against the rule, and starts an agent turn. |
| A service call fails. | The tool returns the error to the model. |

## 19. Security

- The app has administrator access to HA. The scope check is the only barrier
  between an agent and full access. It must have complete test coverage.
- Agents cannot change their definitions.
- Calls that change HA go to an audit log. `admin` changes keep the previous
  version.
- The management UI is available only through Ingress, behind HA login, to HA administrators. It can change any agent definition, so it is as sensitive as HA admin access.
- API keys stay in the app. The UI shows them only masked.

## 20. Example: a heating agent

This example shows how one agent builds use-case logic from the generic
primitives. None of this logic is part of hagent.

**Definition**

- Instructions: keep occupants comfortable at the lowest energy cost. Never
  change the boiler flow temperature by more than 5 °C in one step.
- Inputs: indoor and outdoor temperature, boiler state, presence, and one
  comfort selector for each occupant.
- Outputs: the boiler flow temperature and the radiator valve positions.
- Tools: `self`, `memory`, and `ha: read` on climate, presence and weather
  entities.
- Approval: none at first for outputs. The user can mark outputs for approval
  during a trial period.
- Models: a low-cost chat model and a decision model.

**What the agent builds**

1. An automation calls the agent every 5 minutes. At first each call is an
   agent turn. The agent records warm-up times and occupant preferences in its
   notes.
2. The agent subscribes to the comfort selectors, so that a "cold" report
   starts an invocation at once.
3. The agent subscribes to manual changes of the valves. It treats a manual
   change soon after its own change as a sign of a bad decision, and notes it.
4. The agent writes a rule for the 5-minute calls. The rule asks the decision
   model to choose a flow temperature from a list. Its result template
   computes the weighted mean, applies the 5 °C step limit, and declines when
   confidence is low. The agent turns trial on.
5. In the daily review, the agent reads the trial agreement and the comfort
   reports. It turns trial off when the rule agrees with its own decisions,
   and it changes or deletes the rule when it does not.

## 21. Out of scope for the first version

- Agents that call other agents.
- Access from outside HA, for example through the Model Context Protocol
  (MCP) or the Agent2Agent (A2A) protocol.
- Installation through HACS or an app repository.
- HA installations without the Supervisor, for example a Container install.

## 22. Decided

| Question | Decision |
|---|---|
| Runtime location | An HA app. |
| Providers | OpenAI and OpenRouter. |
| Installation | By hand for the first version. |
| Calculation language for rules | HA templates. |
| Default daily budget | USD 0.50 per agent. |
| Where agents are defined | In the app, through its own management UI. The integration creates one HA device per agent automatically. |
| AI Task entity | Each agent exposes one, with the `GENERATE_DATA`, `SUPPORT_ATTACHMENTS` and `EVALUATE` features. |
| Invocation defaults | `hagent.invoke` waits for the result by default, with a 30-second timeout. Each call can override both. |
| Rule trial sample rate | 1 in 5 matches by default. Each rule can override it. |
| HA user for the app | The app uses the Supervisor proxy, which acts as an administrator. The runtime scope check enforces agent scopes. A separate non-admin HA user is a possible later option. |
| App language | Python 3.15. The official OpenAI SDK serves both providers. |
| Integration language | Python 3.14, the version that HA Core requires, because HA Core loads the integration with its own interpreter. |
| Tooling | mise installs the tools. uv manages Python and dependencies. Backlog.md tracks work. pre-commit runs ruff (lint, static analysis and formatting) and mypy (strict type checking) before each commit. |
| Management UI stack | A React single-page application with TanStack Router, TanStack Query and TanStack Table, built with Vite. The app serves the built files through Ingress. A JSON API supplies data, and a WebSocket sends change events. The router uses the Ingress base path from `X-Ingress-Path`. |

## 23. Open questions

None at present.

## 24. References

- [Jarvis-AIO](https://github.com/sam3gp8/jarvis-aio)
- [HA app communication](https://contributors.home-assistant.io/apps/guides/communication/)
- [HA app configuration](https://contributors.home-assistant.io/apps/reference/configuration/)
- [HA app Ingress](https://contributors.home-assistant.io/apps/guides/ingress/)
- [HA AI Task action](https://www.home-assistant.io/actions/ai_task.generate_data/)
- [HA conversation API](https://developers.home-assistant.io/docs/intent_conversation_api)
- [OpenAI Decisions guide](https://developers.openai.com/api/docs/guides/decisions)
- [OpenRouter decisions endpoint](https://openrouter.ai/docs/guides/features/decisions)
- [Jev on OpenRouter](https://openrouter.ai/docs/guides/community/jev)
