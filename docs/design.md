# hagent design

Status: draft for review. The repository has tooling, but no application code
yet.

## 1. Purpose

hagent is an agent manager for Home Assistant (HA). HA is an open-source home
automation platform. It models devices as entities, and device actions as
services.

An agent is a configured program that uses a large language model (LLM) to
do work. An LLM is a model that reads and writes text. In hagent, the user
defines each agent: its instructions, models, tools and permissions. hagent
runs the agents and keeps their knowledge and history.

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

hagent gives each agent persistent knowledge and history, a tool system with
permissions, and the ability to replace repeated model calls with cheaper
rules that the agent writes itself.

## 3. Terms

| Term | Meaning |
|---|---|
| App | A container that the HA Supervisor runs next to HA Core. HA called apps "add-ons" before release 2026.2. |
| Integration | Python code that HA Core loads. It adds entities, actions and configuration screens to HA. |
| Agent definition | The configuration of one agent, owned by the user (section 7). |
| Agent state | The data that an agent creates and changes itself: rules, schedules and history (section 7). |
| Knowledge store | A named collection of notes and uploaded documents. One or more agents can attach it (section 13.4). |
| Invocation | One call to an agent. |
| Agent turn | An invocation that the LLM handles, with instructions, knowledge, tools and history. |
| Tool | A function that an agent can call during an agent turn. |
| Scope | The limits on what a tool may do for one agent: which HA data and actions it may use, or which knowledge stores it may read or write. |
| Rule | A fast path that an agent writes. A rule answers an invocation without an agent turn (section 10). |
| Version | An unchanging copy of a rule or an agent definition. A change creates a new version (section 10.6). |
| Fallback | The result that an agent gives when it cannot run an agent turn (section 9.7). |
| Decision model | A model that returns typed answers with probabilities, not text. Examples: the OpenAI Decisions API, and Jev on OpenRouter. |
| Embedding model | A model that converts text into a vector, a list of numbers that represents its meaning (section 13.3). |

## 4. Requirements

- R1. Automations call an agent explicitly. This is the primary trigger.
- R2. An agent can schedule its own invocations, from timers or from HA
  events.
- R3. The definition can attach entities or devices as inputs and as outputs.
  An output attachment lets the agent set that entity.
- R4. An agent can return text or structured output to the caller instead.
- R5. Each agent has persistent knowledge and history.
- R6. The user can create several knowledge stores, each with a description
  of what belongs in it. The user can upload documents to a store. Several
  agents can share a store, and one agent can attach several stores.
- R7. An agent can write rules. A rule answers an invocation by a template
  calculation or by one decision model call, without an agent turn.
- R8. An agent wakes up on a schedule to review its rules and change them.
- R9. The definition lists the tools of the agent: self-modification,
  knowledge, history and HA access.
- R10. Each HA tool has a scope: read-only, specific entities or devices, or
  full administration, including changes to automations.
- R11. The definition can expose the agent to Assist. This is optional and
  off by default.
- R12. hagent supports OpenAI and OpenRouter.
- R13. hagent runs as an HA app.
- R14. Cost is a primary design goal.
- R15. The design is generic. No feature exists for one use case only.
- R16. Every default can be overridden, per agent, per rule or per call.
  Installation-wide settings, for example the embedding model, are the
  exception: the user changes them once for the installation.

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
|  |  - AI Task entities            |       |  - tool executor + scopes    | |
|  |  - Assist agents (optional)    |       |  - scheduler                 | |
|  +--------------------------------+       |  - knowledge, history,       | |
|  | Automations, entities, events  |<------|    search (SQLite)           | |
|  +--------------------------------+  HA   |  - provider adapters         | |
|                                      API  |  - management UI (Ingress)   | |
|                                           +--------------+---------------+ |
+----------------------------------------------------------|-----------------+
                                                           | HTTPS
                                                OpenAI / OpenRouter
```

**The integration** is thin. It holds no agent configuration:

- It receives the list of agents from the app, and keeps it current. When
  the user creates, changes or deletes an agent in the app, the integration
  adds, updates or removes the matching device.
- It creates one HA device for each agent, with these entities:
  - a status sensor (idle, running, budget spent, turned off, error);
  - a last-result sensor;
  - a cost-today sensor;
  - an enabled switch;
  - an AI Task entity (section 9.3);
  - a conversation entity, only if the agent allows Assist (section 15).
- It creates one more device for hagent itself, with a connectivity sensor
  that shows the state of the link to the app.
- It provides the `hagent.invoke` action (section 9.2).
- It forwards invocations to the app and returns the results.

**The app** does all agent work:

- It runs outside the HA Core event loop, so a slow model call cannot delay
  HA. It continues to run when HA Core restarts.
- It connects to HA Core through the Supervisor proxy, with the
  `SUPERVISOR_TOKEN` that the Supervisor supplies. It uses the HA WebSocket
  API for state, events and service calls.
- It keeps agent definitions, agent state, knowledge stores and history in
  SQLite, in the app data directory. HA backups include this directory.
- It serves a management UI through Ingress. Ingress shows an app web page
  in the HA sidebar, behind HA login. Only HA administrators can open it.
  Section 7.2 describes the UI.

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

For the first version, the user installs both parts by hand. The user adds
the app from a local app folder, and copies the integration into the HA
`custom_components` folder.

## 7. Ownership of data

The app keeps three kinds of data apart. This separation makes scope
enforcement possible: no agent tool can change data that the user owns.

| Data | Owner | Who changes it |
|---|---|---|
| Agent definitions | The user | The user, in the management UI. Each change creates a new version (section 10.6). |
| Knowledge store configuration: name, description, size limit and attachments | The user | The user, in the management UI. |
| Knowledge store content: files and documents | Shared | Files: agents with `read_write` access, and the user. Documents: the user only. |
| Agent state: rules and schedules | The agent | The agent, through its tools. Each rule change creates a new version. The user can also turn off, delete or roll back rules and schedules. |
| History | The runtime | The runtime records it. Agents and the user only read it. Retention deletes old records (section 13). |

An agent can read its own definition, but no tool writes to it. An agent
therefore cannot widen its own scope, outputs, store access or budget.

### 7.1 Agent definition

| Field | Content |
|---|---|
| Name and instructions | What the agent is for, in plain text. |
| Models | A chat model; an optional decision model; and an optional low-cost model for compaction summaries. The summary model defaults to the chat model. Each model has a provider. |
| Inputs | Entities or devices whose current values go into every invocation. |
| Outputs | Entities that the agent may set. |
| Result type | `none`, `text`, or `structured` with a JSON schema. |
| Tools and scopes | Section 8. |
| Knowledge stores | The attached stores, with access and main-file injection for each (section 13.4). |
| Retrieval | Automatic recall: `off` (default), `assist` or `all`, with the result count and token limit (section 13.3). |
| Approval | Tools or scopes that need the user's approval for each call (section 8). |
| Assist | Off by default (section 15). |
| Budget | A daily cost limit, USD 0.50 by default, and limits for one agent turn: maximum tool calls (default 20) and maximum tokens (section 17). |
| Concurrency | How the agent handles an invocation that arrives while another runs: `queued` (default), `single`, `restart` or `parallel` (section 9.6). |
| Fallback | What the agent returns when it cannot run an agent turn: `last_result` (default), `fixed` with a value, `no_change`, or `error` (section 9.7). |
| Scheduling limits | Minimum interval, maximum schedules and subscriptions, and debounce (section 12). |
| Review schedule | When the agent receives a review invocation. The default is daily (section 14). |

When the user creates an agent, hagent also creates its private knowledge
store (section 13.4). When the user deletes an agent, the management UI asks
whether to delete or keep the private store.

### 7.2 Management UI

The management UI is a web application in the app. It has four areas.

**Agents**

- Create, edit, copy, turn off and delete agents.
- Pick inputs, outputs and HA scopes from the live list of HA entities,
  devices, areas and labels.
- Turn tool groups on and off, attach knowledge stores, and mark tools for
  approval.
- Choose models, and set the budget, limits, retrieval and review schedule.
- Run a test invocation with sample data, without changes to HA.
- See earlier versions of the definition, compare them, and restore one.

**Knowledge**

- Create stores, and edit their names, descriptions and size limits.
- Upload, replace and delete documents.
- Browse and edit files, see which agents attach each store, and restore
  earlier versions of files.

**Inspect**

- Invocations: a list with filters by agent, source, path and result. Each
  entry opens a full trace: inputs, the rule or the model messages, every
  tool call with its scope decision, each compaction, the result and the
  cost.
- History: search across past invocations.
- Rules: each rule with its versions, templates, context, statistics per
  version, backtest results and trial agreement. The user can turn a rule
  off, delete it or roll it back to an earlier version.
- Schedules and subscriptions: what is active, and when each one last ran.
- Permissions: the effective scope of each tool, resolved to entity IDs, and
  a log of denied calls.
- Changes to HA: the audit log, with the previous version of each `admin`
  change.
- Cost: by agent, by day and by path, including cached tokens and embedding
  cost.
- Approvals: pending approval requests, as an alternative to the HA
  notification.

**Settings**

- Providers and API keys.
- The embedding model for the installation (section 13.3).
- The installation-wide daily budget (section 17).
- The HA users who can approve tool calls (section 8).

## 8. Tools and scopes

The definition turns on tool groups. hagent supplies four groups.

**`self`** — change agent state:

- Create, change, reorder, turn on, turn off and delete rules.
- Run backtests, activate rule versions and roll rules back (sections 10.6
  and 10.7).
- Set, list and cancel timers and cron schedules, including extra reviews.
- Subscribe to and unsubscribe from HA events and state changes, with a
  filter.

**`knowledge`** — knowledge stores (section 13.4): list, read, search,
write and delete. The scope is the set of attached stores, with the access of
each attachment.

**`memory`** — history:

- Search history and conversations, with filters and hybrid ranking
  (`memory.search`, section 13.3).
- Read rule statistics (section 10.4).

**`ha`** — Home Assistant, at one of four levels:

| Level | Permitted |
|---|---|
| `none` | Nothing. The agent receives only its inputs. |
| `read` | Read states, history and statistics, and find entities by description (`ha.find_entities`). The scope limits this to listed entities, devices, areas, labels or domains, or allows all. |
| `control` | `read`, plus service calls on permitted targets. The scope can also list permitted services. |
| `admin` | Full access to HA: all entities, all services, and changes to HA configuration, including automations, scripts, helpers, scenes and dashboards. `admin` has no narrower form. |

An output attachment is a short form of `control` for one entity.

`admin` is a blanket scope on purpose. An agent that can write automations
or scripts can make HA do anything, because HA runs them with full rights.
A narrowed `admin` would therefore be a false limit.

**Rules for `read` and `control`.** The runtime applies these checks:

- **Resolved targets.** Before each check, the runtime resolves area, label,
  device and floor targets to entity IDs. It checks each entity ID against
  the scope.
- **Service data.** The runtime also checks entity IDs inside the service
  data, not only in the target. A call that names an entity outside the
  scope fails.
- **Indirect services.** Some services run other actions, so they can reach
  entities outside the scope. The runtime denies these services unless the
  scope permits each one by name: `script.*`, `automation.*`, `scene.*`,
  `homeassistant.*` (the generic services), `shell_command.*`,
  `rest_command.*`, `python_script.*`, `pyscript.*`, `hassio.*` and
  `notify.*`.
- **Scope changes.** A scope that uses areas, labels or devices changes when
  the user moves an entity. The runtime resolves each scope again when the HA
  registries change. If a scope grows, the runtime sends an HA notification
  and shows the change in the management UI.

**Enforcement.** Through the Supervisor proxy, the app has administrator
access to HA. HA tokens have no fine-grained scopes. The runtime therefore
checks every tool call against the scope before it acts. This applies to HA
calls and to knowledge store calls. The runtime, not the instructions,
enforces the scope. Tests must show that a call outside the scope never
reaches HA or the store.

**Approval.** For a tool or scope that the definition marks for approval, the
runtime sends an HA notification with accept and reject actions to the
approvers in the settings. Only those HA users can approve. The agent turn
waits for the answer, up to a time limit. Rules cannot wait for
approval, so a rule cannot write an output that needs approval (section
10.1).

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

The action targets the agent device, or any entity of that device.

| Field | Meaning |
|---|---|
| `data` | Optional context, as a mapping. |
| `wait` | `true` (default): return the result as response data. `false`: return at once, and deliver the result later. |
| `timeout` | Maximum wait in seconds. The default is 30. |
| `on_timeout` | `cancel`: stop the invocation, write nothing, and return an error. `continue`: finish in the background. The default is `cancel` for agents with outputs, and `continue` for other agents. |

When `wait` is true, the action returns response data:

```yaml
result: ...          # text or structured result, per the result type
writes: [...]        # output entities that this invocation set
path: rule           # rule, agent_turn or fallback
rule_id: r12         # if a rule answered
cost_usd: 0.0000     # cost of this invocation
```

When `wait` is false, or the timeout expires with `on_timeout: continue`,
hagent finishes in the background. It then fires an `hagent_result` event
and updates the last-result entity of the agent.

If the app is not reachable, the action fails with an error. The automation
can catch it with `continue_on_error`. The connectivity sensor shows the
state of the link.

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
keeps its own knowledge stores and history across all invocations.

### 9.4 The router

The router handles each invocation in this sequence:

1. If the agent is turned off, it rejects the invocation with an error.
2. It applies the concurrency mode of the agent (section 9.6).
3. It collects the input values.
4. It tests the agent's active rules in the order that the agent set. The
   first rule that matches answers the invocation (section 10).
5. If no rule matches, or the rule declines, it starts an agent turn. If it
   cannot run an agent turn, it returns the fallback (section 9.7).
6. It records the invocation, the path, the result, the cost, the version of
   the definition and the version of the rule (section 10.6).

Review and Assist invocations always start an agent turn.

### 9.5 Agent turn

Pydantic AI runs the agent turn. Pydantic AI is a Python framework for LLM
agents. It supplies the tool loop, typed tool definitions, structured
results, context compaction and provider prompt caching. hagent supplies the
tools, the scope check, the stored history and the cost records.

An agent turn sends the model these parts, in this order. The most stable
parts come first, so that the provider prompt cache works (section 13.2):

1. The instructions from the definition.
2. The tool definitions.
3. The attached knowledge stores, with descriptions and access, the index of
   their contents, and the injected main files (section 13.4).
4. A short description of the agent's rules, schedules and scopes.
5. Recent history for this source: a running summary plus the latest
   records, within a size limit (section 13.1).
6. Automatic recall results, only if the definition turns recall on
   (section 13.3).
7. The invocation: source, current time, inputs and caller data.

The model then calls tools in a loop until it returns a result. The runtime
checks each tool call against the scope, and records it. The turn stops when
it reaches the limits in the definition: maximum tool calls and maximum
tokens. The runtime then returns the fallback.

### 9.6 Concurrency

Invocations for one agent can arrive at the same time, for example from an
automation, a subscription and the review. Each agent has one queue. The
concurrency mode in the definition sets what happens when an invocation
arrives while another one runs. The modes have the same names and meanings
as HA automation modes:

| Mode | Behaviour |
|---|---|
| `queued` (default) | The new invocation waits, and runs when the current one ends. The queue holds 10 invocations by default. |
| `single` | The new invocation fails at once with an error. |
| `restart` | The runtime cancels the current invocation, and starts the new one. A cancelled invocation writes nothing more. |
| `parallel` | Both run. The user chooses this only for agents with no outputs. |

Assist conversations have their own queue for each conversation, so that a
person does not wait for background work.

Changes to rules, schedules and the definition take effect between
invocations, never during one. An invocation uses the versions that were
active when it started.

The runtime keeps the queue in SQLite. When the app starts again after a
stop, it runs the queued invocations. An invocation that was running when
the app stopped does not resume: the runtime records it as failed and
returns the fallback.

### 9.7 Fallback

The router returns the fallback when it cannot run an agent turn. This
happens when:

- the daily budget of the agent, or the installation budget, is spent;
- the chat model provider is not reachable, for example because the internet
  connection is down;
- the agent turn reaches its tool-call or token limit;
- the agent turn fails after retries.

The definition sets the fallback:

| Fallback | Result |
|---|---|
| `last_result` (default) | The result of the last successful invocation from the same source. The runtime does not repeat its writes. If no earlier result exists, the router returns an error. |
| `fixed` | A value from the definition. The runtime applies its writes, if any. |
| `no_change` | No result and no writes. The caller receives an empty result. |
| `error` | An error to the caller. |

The response data and the history mark the path as `fallback`, with the
reason. The status sensor shows the reason. Rules do not need the internet
when they use only template actions, so they continue to work when the
connection is down.

## 10. Rules

A rule is a fast path that the agent writes with the `self` tools. Rules let
an agent answer repeated invocations at low or zero cost.

### 10.1 Rule structure

| Part | Content |
|---|---|
| State | `draft`, `active` or `off`. Only active rules answer invocations. A new version starts as `draft` (section 10.7). |
| Order | The position of the rule in the agent's list. The router tests rules in this order. |
| Match | A template that returns true when the rule applies. It can test the source, the inputs, the caller data and the context. |
| Context | Optional data that the runtime collects before the action runs (section 10.5). |
| Action | `template` or `decision` (below). |
| Result | A template that builds the final result from the action output. It can also return "decline", which passes the invocation to an agent turn. |
| Writes | Optional output entities to set from the result. They must be in scope, and must not need approval. |
| Cache | Optional. A time window and a list of inputs. If an invocation arrives with the same values for those inputs within the window, the runtime returns the previous result without running the action. Off by default. |
| Agreement | A template that compares the rule result with an agent turn result, and returns true when they agree. For example: "the two flow temperatures differ by 3 °C or less". Backtests and trials use it. |
| Trial | Optional. When on, the runtime also runs an agent turn on a sample of matches, and compares the two results with the agreement template. The trial agent turn writes nothing: only the rule result takes effect. The default sample rate is 1 in 5. Each rule can set its own rate. Trial agent turns count towards the budget, and stop when the budget is spent. |

### 10.2 Template action

The action is a template. All rule templates (match, action, result and
agreement) use HA template syntax: Jinja with the common HA functions, for
example `states()`, `state_attr()`, `is_state()`, `now()` and the HA
filters.

The app renders the templates itself, in a sandboxed Jinja environment. It
does not send them to HA Core. The template functions read the app's state
cache, filtered to the agent's `ha` scope. A template therefore cannot read
an entity outside the scope: for that entity, `states()` returns `unknown`.
An agent with `ha: none` sees only its inputs.

The runtime passes these variables to each template: the invocation source,
the input values, the caller data and the context (section 10.5). The result
template also receives the action output, for example the decision
probabilities.

The sandbox supports a documented subset of HA template functions. The
management UI shows an error when a rule uses a function outside the subset.
Rules keep working when HA Core restarts, because they do not depend on HA
to render.

Example: "Is anyone home?" computed from presence entities.

### 10.3 Decision action

The action sends one request to the decision model in the definition. The
request has one or more questions. Each question has:

- The question text, written by the agent.
- A list of choices: yes/no, categories or numbered options.
- The values to include, taken from the inputs, the caller data and the
  context.

The decision model returns a probability for each choice of each question.
The result template turns the probabilities into the result. For example,
it can take the most likely choice, compute a weighted mean, or decline when
no choice is likely enough. The agent writes this logic. hagent does not
build it in.

The OpenAI Decisions API and the OpenRouter decisions endpoint both return
typed answers with probabilities. hagent puts both behind one adapter. The
OpenRouter decisions endpoint is in alpha, so the adapter must expect
changes.

### 10.4 Rule statistics

The runtime records, for each version of each rule:

- How often it matched, answered, declined and used its cache.
- Errors.
- Cost.
- Backtest results (section 10.7).
- Trial agreement, if trial is on.

Agents read these statistics with the `memory` tools. hagent does not
promote, demote or delete rules by itself. The agent does this, usually in a
review.

### 10.5 Rule context

Many decisions need more than the current input values. For example, a
heating decision needs the last 24 hours of temperatures and the weather
forecast. A rule can therefore declare context items. The runtime collects
them before the action runs, and passes them to the templates and to the
decision request.

| Context item | Content |
|---|---|
| `history` | Statistics for entities in scope over a time window, for example hourly mean, minimum and maximum for 24 hours. |
| `action_response` | The response of an HA action that only returns data, for example `weather.get_forecasts`. The action and its target must be in the agent's `ha` scope. |
| `store_file` | The content of a file in an attached knowledge store, for example the main file. |
| `invocation_history` | A summary of the agent's recent invocations from the same source. |

Each item can set a cache time, so that a rule called every 5 minutes does
not fetch a 24-hour history every time. The scope check applies to every
item.

### 10.6 Versions

Rules and agent definitions have versions.

- A version never changes. A change to a rule or a definition creates a new
  version.
- Statistics belong to one rule version. A new version starts with new
  statistics.
- Each history record stores the definition version and the rule version
  that produced it.
- The agent can roll a rule back with the `self` tools. The user can roll a
  rule or a definition back in the management UI.

### 10.7 Backtesting

A backtest runs a rule version against recorded history before the rule
answers live invocations.

1. The runtime selects past invocations that the rule matches, from the
   agent's history. The default is the last 7 days.
2. For each one, it runs the rule action with the recorded inputs, caller
   data and context. It writes nothing.
3. It compares each rule result with the recorded agent turn result, with the
   agreement template.
4. It stores the agreement rate and the cost in the rule statistics.

A new rule version starts in the state `draft`. The agent runs a backtest,
reads the result, and then activates the rule or changes it. A backtest of a
template rule costs nothing. A backtest of a decision rule costs one decision
request for each past invocation, so the agent can limit the number of
invocations that it tests. Backtests need the inputs and context of each past
invocation, so history keeps them for the retention period, 30 days by
default.

## 11. Outputs and results

- **Output entities.** The agent sets an output entity with a tool call in an
  agent turn, or through the writes of a rule. The runtime checks the scope.
  hagent adds no limits beyond the scope and approval. An agent that needs
  limits, for example a maximum change per step, writes them into its rules
  and into its notes in a knowledge store. The user can also enforce limits
  in HA.
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

**Own writes.** Each service call that the runtime makes carries an HA
context ID. HA copies this ID into the state changes that the call causes.
By default, a subscription ignores state changes that the agent caused
itself. An agent therefore cannot start itself through its own outputs.
The runtime also marks each state change in history as `agent` or
`external`, so that an agent can tell a person's change from its own.

**Missed schedules.** If the app was stopped when a schedule was due, the
schedule runs once when the app starts, if it is less than 10 minutes late.
Otherwise the runtime skips it and records the skip.

APScheduler runs timers and cron schedules. APScheduler is a Python library
for scheduled jobs. It keeps jobs in SQLite through its SQLAlchemy job store,
and restores them when the app starts. hagent runs event subscriptions
itself, from the HA WebSocket event stream, because APScheduler has no HA
event source.

## 13. Knowledge and history

An agent remembers through two kinds of data:

- **Knowledge stores** hold notes that agents write, and documents that the
  user uploads. Each agent has a private store, and can attach shared stores
  (section 13.4).
- **History** is a record of each invocation: its inputs, path, tool calls,
  result and cost. Each agent has its own history. The agent searches it with
  the `memory.search` tool. A daily task summarises old history and deletes
  raw records after a retention period, 30 days by default. Retention does
  not apply to knowledge stores.

### 13.1 Context compaction

Compaction makes the context smaller before it goes to the model. hagent
uses the compaction strategies in Pydantic AI, in three places:

- **Inside an agent turn.** A tool loop can collect large tool results. The
  runtime cuts each tool result to a size limit, and keeps the full result in
  history. When the turn reaches a token limit, Pydantic AI compacts the older
  part of the turn.
- **Across turns of one conversation or source.** An Assist conversation, or
  a series of invocations from one source, keeps a running summary plus the
  latest records, instead of a full transcript. The summary model in the
  definition writes the summary.
- **Main files.** When an injected main file (section 13.4) reaches 80
  percent of its size limit, the runtime asks the agent to move detail into
  other files and rewrite the main file shorter. The runtime does not cut the
  file itself.

The invocation trace in the management UI shows each compaction and the
summary that replaced the original text.

### 13.2 Caching

hagent uses four caches:

- **Provider prompt cache.** Providers charge less for a prompt that starts
  with the same text as a recent prompt. The agent turn therefore puts the
  most stable parts first and the parts that change last (section 9.5). The
  current time and input values go last, so that they do not stop the cache
  from working. OpenAI caches automatically. Through OpenRouter, some models
  need cache markers, which Pydantic AI adds. Pydantic AI also reports when a
  request misses the cache unexpectedly. The cost records keep the cached
  token count for each call.
- **Rule result cache.** A rule can return its previous result for the same
  inputs within a time window (section 10.1).
- **HA state cache.** The app subscribes to HA state changes, so it keeps a
  current copy of entity states. State reads by tools and rule templates use
  this copy, not a request to HA, so both see the same state in one
  invocation.
- **Query cache.** Within one agent turn, the runtime keeps the results of
  history and statistics queries, so that a repeated query does not go to HA
  again.

Compaction changes the start of the conversation, so it can stop the
provider prompt cache from working for one request. The runtime compacts
only when the saving is larger than this cost.

### 13.3 Retrieval

Retrieval finds stored text that is relevant to a question, and gives it to
the model. Retrieval-augmented generation (RAG) is the name for this method.
hagent uses hybrid search: it runs a keyword search and a vector search on
the same query, and merges the two result lists.

- **Keyword search** uses FTS5, the full-text index that SQLite includes.
- **Vector search** uses sqlite-vec, a SQLite extension. The embedding model
  converts each text into a vector. Texts with similar meaning have vectors
  that are near each other.
- **Merge** uses reciprocal rank fusion (RRF). RRF gives each result a score
  from its rank in each list, and adds the scores.

All indexes are in the app SQLite database. No separate vector database is
necessary.

**What hagent indexes**

| Collection | Content | Who can search it |
|---|---|---|
| Knowledge stores | Files and documents in each store, split into passages at headings, paragraphs and pages (section 13.4). | Agents attached to the store. |
| History | A short text for each invocation (source, decision, result), review reports and compaction summaries. | One agent. |
| Conversations | Assist messages. | One agent. |
| Entities | The HA entity catalogue: names, aliases, areas, labels, domains and device names. | All agents. Each query is filtered to the agent's `ha` scope. |

**How agents use it**

- `knowledge.search` searches the knowledge stores that the agent can read
  (section 13.4).
- `memory.search` searches history and conversations. It accepts filters for
  time range, source and record type, so that it combines exact filters with
  ranked search.
- `ha.find_entities` finds entities from a description, for example "the
  radiator in the back bedroom". It returns only entities in the agent's
  scope.

**Agent-driven retrieval.** By default, the agent decides when to search.
The always-loaded context tells it what it can search: the store
descriptions, the index of store contents and the injected main files
(section 13.4). This follows current practice: a small core memory is always
present, and the agent retrieves more on demand.

**Automatic recall.** The definition can turn on automatic recall. The
runtime then searches all of the agent's collections with the invocation
text before an agent turn. It adds the best results to the prompt, after the
history and before the invocation. The setting has three values:

- `off` (default);
- `assist`: only for Assist conversations, where a person's question is a
  good search query;
- `all`: for every agent turn.

The defaults are 5 results and a 1,000-token limit. Recall is off by default
because invocations from automations are often short and repetitive. Their
text makes a poor search query, and recall would add the same passages to
every turn. Rules never use recall.

**Embeddings**

- One embedding model serves the whole installation, so that all vectors are
  comparable. The user selects it in the management UI settings, from OpenAI
  or OpenRouter. A change of model starts a full re-index in the background.
- The runtime creates embeddings when it writes a record, in batches, from a
  background queue. It does not create an embedding again for text that has
  the same content hash.
- Embedding cost of text that an agent writes counts towards that agent's
  budget. Uploaded documents and the entity catalogue are charged to the
  installation.
- Embedding sends text to the provider. The management UI states this when
  the user selects the model.

**Failure and limits**

- If sqlite-vec does not load, or the embedding provider fails, search falls
  back to keyword search only. The status sensor shows the reason.
- sqlite-vec is before version 1.0, so its SQL interface can change. hagent
  keeps all sqlite-vec statements in one module, and pins the version.
- Retention deletes vectors and index entries with their records.

### 13.4 Knowledge stores

A knowledge store is a named collection of text that agents can read, and
that some agents can write. The user creates and manages stores in the
management UI.

**Store properties**

| Property | Content |
|---|---|
| Name | A short identifier, for example `heating` or `household`. |
| Description | What belongs in the store, and what does not. For example: "Facts about the heating system: equipment, room warm-up times, settings that worked. Not occupant preferences." The runtime shows the description to every attached agent, so that agents write each fact to the correct store. |
| Size limit | The maximum total text. The default is 1 MB. |

A store holds two kinds of content:

- **Files**: Markdown text that agents and the user write and edit. A store
  can have a main file, `MAIN.md`.
- **Documents**: files that the user uploads, for example a boiler manual.
  Agents can read and search documents, but cannot change them.

**Private stores.** Each agent has a private store, with `read_write` access
(section 7.1). It is where the agent keeps its own notes. The user can edit
its description, and can attach it to other agents too.

**Attachments.** The agent definition lists the stores that the agent can
use. Each attachment has:

| Setting | Values |
|---|---|
| Access | `read` or `read_write`. |
| Inject main file | If on, `MAIN.md` of the store goes into each agent turn. The default is on for the private store and off for shared stores. |

One agent can attach many stores, and one store can attach to many agents.
An agent cannot attach a store to itself, or change its own access. Only the
user does this, in the management UI.

**Tools.** The `knowledge` tool group serves all stores. hagent does not use
the memory tools of the Pydantic AI Harness package, because they serve one
notebook and have no store parameter. hagent copies their write behaviour:

| Tool | Effect |
|---|---|
| `knowledge.list` | List the attached stores, with descriptions and access, and the files and documents in a store. |
| `knowledge.read` | Read a file or a document, or one section of it. Large documents are read in parts. |
| `knowledge.search` | Hybrid search across all readable stores, or the stores named in the call. Each result names its store, its file or document, and its location. |
| `knowledge.write` | Create a file, append to a file, or replace one exact passage. Needs `read_write` access. |
| `knowledge.delete` | Delete a file. Needs `read_write` access. Agents cannot delete documents. |

The runtime checks store access on every call (section 8).

**Prompt.** Each agent turn includes, after the tool definitions:

- the list of attached stores, with their descriptions and access;
- an index of each store: the titles of its files and documents, with no
  content, so that the agent knows what it can search or read;
- the main files that the attachments inject.

The index is sorted by name, so that a change to one file does not move the
other entries. If the index of a store is larger than a limit, 50 entries by
default, the runtime shows the first entries and the total count. This part
of the prompt changes only when files are added, renamed or removed, or when
attachments change, so it seldom stops the provider prompt cache from
working.

**Shared writes.** Several agents can write the same store. Each write
checks the version of the file that the agent read. If another agent changed
the file in the meantime, the write fails, and the agent must read the file
again. Each change records the agent or user that made it. The management UI
shows the change history of each file, and can restore an earlier version.

**Documents**

- The user uploads documents in the management UI. The first version accepts
  PDF files with a text layer, Markdown, plain text and HTML.
- The app extracts the text, splits it into passages at headings and pages,
  and indexes it. A search result names the document and the page.
- The user can replace a document with a new version, or delete it. The app
  updates the index.

**Example.** A heating agent attaches three stores:

- its private store (`read_write`, main file injected);
- `heating` (`read_write`), shared with a hot-water agent, which holds the
  boiler manual and notes about the heating equipment;
- `household` (`read`), which holds facts about the occupants and their
  routines, written by a separate household agent.

## 14. Review

A review is an invocation with the source `review`. The definition sets the
schedule, daily by default. An agent can also schedule extra reviews with the
`self` tools.

hagent does not define what a review does. The instructions and the
knowledge stores of the agent define it. A typical review reads rule
statistics and recent history, then changes rules and store files.

## 15. Assist exposure

If the definition allows it, the integration adds a conversation entity to
the agent device. HA then lists the agent as an Assist conversation agent.
A person can then select the agent in an Assist pipeline, or talk to it in
the HA chat window.

A conversation is a series of agent turns with the same knowledge stores,
history, tools and scopes. The app keeps the conversation history, with
compaction (section 13.1), so it does not expire after 5 minutes. Rules do
not answer Assist invocations.

## 16. Providers

- **OpenAI**: chat models through the Responses API, the Decisions API, and
  embeddings.
- **OpenRouter**: chat models through the OpenAI-compatible API, the
  decisions endpoint, which provides Jev, and embeddings.

Pydantic AI supplies the chat model adapters for both providers. hagent
writes its own adapters for the decision endpoints, because Pydantic AI does
not support them. hagent calls the embeddings endpoints of both providers
through their OpenAI-compatible interface. Each adapter reports token use,
cached tokens and cost for each call.

The user enters API keys in the management UI settings. The app never sends
keys to HA and never writes them to logs.

## 17. Cost controls

- Rules answer repeated invocations without an agent turn.
- Decision actions bill input tokens only.
- Rule result caches skip repeated rule actions.
- The prompt order lets providers cache the stable part of each prompt.
- Knowledge stores, running summaries and compaction replace large context
  rebuilds.
- Retrieval is agent-driven, so that no turn pays for passages that it does
  not need.
- Embeddings are created once for each unique text.
- Each agent has a daily budget. The default is USD 0.50. When the budget is
  spent, agent turns stop, rules continue, and the router returns the
  fallback (section 9.7). The status sensor shows the reason.
- The installation has a daily budget too, set in the management UI. It also
  covers costs that belong to no agent, for example document embeddings.
- Each agent turn has a maximum number of tool calls and tokens.
- Before each model call, the runtime reserves the expected cost against the
  budget, so that parallel turns cannot spend more than the budget.
- Limits on self-scheduled invocations prevent runaway cost.
- Cost sensors in HA show the cost per agent per day.

## 18. Failure handling

| Failure | Behaviour |
|---|---|
| The app stops. | `hagent.invoke` and the AI Task entities fail with an error. The connectivity sensor turns off. When the app starts again, it runs queued invocations and records running ones as failed (section 9.6). |
| HA Core restarts. | The app reconnects and restores subscriptions. Rules with template actions continue to work during the restart. |
| The internet connection is down. | Template rules continue. Decision rules decline. Agent turns return the fallback (section 9.7). |
| A decision call fails. | The runtime retries with backoff. If it still fails, the rule declines, and the router starts an agent turn. |
| A chat model call fails. | The runtime retries with backoff. If it still fails, the router returns the fallback. |
| The embedding provider fails. | Search falls back to keyword search. The runtime embeds the queued texts later. |
| A rule fails. | The runtime records the error against the rule, and starts an agent turn. |
| A service call fails. | The tool returns the error to the model. |
| The budget is spent. | Rules continue. Agent turns return the fallback until the next day. |

## 19. Security

- The app has administrator access to HA. The scope check is the only barrier
  between an agent and full access. It must have complete test coverage.
- `admin` is a blanket scope. The user gives it only to agents that the user
  trusts with full control of HA.
- Rule templates render in the app against a scoped view of state, so a rule
  cannot read entities outside the agent's scope (section 10.2).
- The scope check resolves targets to entity IDs, checks service data, and
  denies indirect services such as scripts and automations unless the scope
  names them (section 8).
- Agents cannot change their definitions, their store attachments or their
  access.
- Calls that change HA go to an audit log. `admin` changes keep the previous
  version.
- Text from outside the instructions can contain instructions that an
  attacker planted. This includes uploaded documents, HA entity names, store
  files that other agents wrote, and recalled history. The runtime marks such
  text as data, not instructions. Store change history lets the user find and
  undo a bad write.
- The management UI is available only through Ingress, behind HA login, to
  HA administrators. It can change any agent definition, so it is as
  sensitive as HA admin access.
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
- Tools: `self`, `memory`, `knowledge`, and `ha: read` on climate, presence
  and weather entities.
- Knowledge stores: the three stores in the example in section 13.4.
- Approval: none. During a trial period, the user can mark outputs for
  approval. Rules cannot write those outputs, so every decision then goes
  through an agent turn until the user removes the approval.
- Concurrency: `queued`.
- Fallback: `last_result`, so that the heating keeps its last setting when
  the budget is spent or the internet connection is down.
- Models: a low-cost chat model and a decision model.

**What the agent builds**

1. An automation calls the agent every 5 minutes. At first each call is an
   agent turn. The agent records room warm-up times in the `heating` store,
   and reads occupant routines from the `household` store. It looks up the
   boiler limits in the boiler manual when it needs them.
2. The agent subscribes to the comfort selectors, so that a "cold" report
   starts an invocation at once.
3. The agent subscribes to changes of the valves. The runtime ignores the
   agent's own writes, by their HA context IDs, so the agent sees only manual
   changes. It treats a manual change soon after its own change as a sign of
   a bad decision, and records it in its private store.
4. The agent writes a rule for the 5-minute calls. Its context collects the
   last 24 hours of temperatures, cached for 30 minutes, and the weather
   forecast, cached for 1 hour. The rule asks the decision model to choose a
   flow temperature from a list. Its result template computes the weighted
   mean, applies the 5 °C step limit, and declines when confidence is low.
   Its agreement template accepts a difference of 3 °C or less.
5. The agent backtests the rule against the last 7 days of its own
   decisions. The rule agrees on 92 percent of them, so the agent activates
   it with trial on.
6. In the daily review, the agent reads the trial agreement, the comfort
   reports and the manual changes. It turns trial off when the rule agrees
   with its own decisions. When it does not, it writes a new rule version,
   backtests it, and activates it or rolls back.

## 21. Out of scope for the first version

- Agents that call other agents.
- Access from outside HA, for example through the Model Context Protocol
  (MCP) or the Agent2Agent (A2A) protocol.
- Installation through HACS or an app repository.
- HA installations without the Supervisor, for example a Container install.
- A local embedding model inside the app.
- Text extraction from scanned PDFs and images (optical character
  recognition).
- Image generation through the AI Task entity.

## 22. Decided

| Question | Decision |
|---|---|
| Runtime location | An HA app. |
| Providers | OpenAI and OpenRouter. |
| Installation | By hand for the first version. |
| Where agents are defined | In the app, through its own management UI. The integration creates one HA device per agent automatically. |
| AI Task entity | Each agent exposes one, with the `GENERATE_DATA`, `SUPPORT_ATTACHMENTS` and `EVALUATE` features. |
| Calculation language for rules | HA template syntax (Jinja with HA functions), rendered by the app in a sandbox against a state view limited to the agent's scope. |
| `admin` scope | A blanket scope: full access to HA, with no narrower form. |
| Indirect services | Denied for `read` and `control` scopes unless the scope names each one (section 8). |
| Prompt injection | No measures beyond marking retrieved text as data. |
| Concurrency | A per-agent queue with HA automation modes. `queued` by default. |
| Fallback | `last_result` by default. Used when the budget is spent, the provider is unreachable, or a turn fails or reaches its limits. |
| Rule quality | Agreement templates, backtests against history before activation, and trials whose agent turns write nothing. |
| Versions | Rules and definitions have unchanging versions, statistics per version, and rollback. |
| Rule context | Rules can collect history statistics, data-only action responses, store files and invocation summaries. |
| Own writes | Recognised by HA context IDs, and ignored by subscriptions by default. |
| Timeout | `on_timeout: cancel` by default for agents with outputs. |
| Approval of rules | No user approval before a rule goes live. |
| First version scope | All features in this document. No phased release. |
| Default daily budget | USD 0.50 per agent. |
| Invocation defaults | `hagent.invoke` waits for the result by default, with a 30-second timeout. Each call can override both. |
| Rule trial sample rate | 1 in 5 matches by default. Each rule can override it. |
| HA user for the app | The app uses the Supervisor proxy, which acts as an administrator. The runtime scope check enforces agent scopes. A separate non-admin HA user is a possible later option. |
| Python version | Python 3.14 for the app and the integration. HA Core requires 3.14 and loads the integration with its own interpreter. The app uses the same version for consistency. |
| Agent framework | Pydantic AI runs agent turns: tool loop, structured results, compaction and prompt caching, for both providers. hagent keeps history storage, scopes, rules, knowledge stores and decision models. |
| Scheduler | APScheduler, with its SQLAlchemy job store in SQLite. |
| Knowledge | Knowledge stores: several per agent, shareable between agents, each with a description, with agent-written files and user-uploaded documents. |
| Retrieval | Hybrid search (SQLite FTS5 plus sqlite-vec, merged with RRF) over stores, history, conversations and the HA entity catalogue, in the first version. Agent-driven by default. Automatic recall is optional and off by default. |
| Embedding model | A low-cost model available through both providers, for example OpenAI `text-embedding-3-small`. Check the current models before release. |
| Local embedding model | Not in the first version. |
| Management UI stack | A React single-page application with TanStack Router, TanStack Query and TanStack Table, built with Vite. The app serves the built files through Ingress. A JSON API supplies data, and a WebSocket sends change events. The router uses the Ingress base path from `X-Ingress-Path`. |
| Tooling | mise installs the tools. uv manages Python and dependencies. Backlog.md tracks work. pre-commit runs ruff (lint, static analysis and formatting) and mypy (strict type checking) before each commit. |

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
- [OpenRouter embeddings API](https://openrouter.ai/docs/api-reference/embeddings)
- [Pydantic AI compaction](https://pydantic.dev/docs/ai/capabilities/compaction/)
- [Pydantic AI caching](https://pydantic.dev/docs/ai/capabilities/caching/)
- [Pydantic AI OpenRouter models](https://pydantic.dev/docs/ai/models/openrouter/)
- [Pydantic AI Harness memory](https://pydantic.dev/docs/ai/harness/memory/) (model for the write behaviour of the `knowledge` tools)
- [APScheduler](https://apscheduler.readthedocs.io/)
- [sqlite-vec](https://github.com/asg017/sqlite-vec)
