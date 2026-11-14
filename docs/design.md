# hagent design

Status: draft for review. No code exists yet.

## 1. Purpose

hagent hosts persistent agents for Home Assistant (HA). HA is an open-source
home automation platform. It models devices as entities, and device actions as
services.

An agent is a configured program that uses a large language model (LLM) to
make decisions. An LLM is a model that reads and writes text. In hagent, an
agent has its own instructions, inputs, outputs, tools, memory and history.

HA automations call hagent agents. An automation is an HA rule of the form
"when a trigger occurs, if conditions are true, run actions." The agents work
in the background. They supply decisions to automations, or they control
entities directly. A person can also talk to an agent, but only if the agent
definition allows it.

hagent must be generic. Heating control is the first use case, but the design
must not depend on it.

## 2. Problem

HA has AI features, but each call starts from zero:

- The `ai_task.generate_data` action returns structured data. It does not take
  a previous conversation as input.
- The `conversation.process` action continues a conversation, but HA deletes
  the session after 5 minutes without activity.

The owner built two heating controllers with these features. Each controller
is one automation:

- One automation runs every 5 minutes. It collects 24 hours of history, 72
  hours of occupant comfort feedback and a weather forecast. It sends all of
  this to a decision model with 16 possible flow temperatures. It converts the
  returned probabilities to one value. Then it applies a clamp, a step limit
  and on/off hysteresis, and writes the result.
- One automation runs every 15 minutes. It collects 10 days of hourly history
  and sends it to a large model. The model returns a structured result for the
  boiler and for several radiator valves, plus warnings and suggestions.
- A separate script sends a valve command again until the valve reports the
  requested position.

These automations show four gaps:

1. Each run rebuilds all context from raw history. Nothing persists between
   runs. Each run therefore pays for the full context again.
2. Each automation contains its own safety logic: clamps, step limits,
   hysteresis and retries.
3. Each automation sends every question to a model, including repeated
   questions that a simple calculation can answer.
4. Nothing checks if past decisions were good, and nothing changes the logic
   when they were not.

hagent closes these gaps for any agent, not only for heating.

## 3. Terms

| Term | Meaning |
|---|---|
| App | A container that the HA Supervisor runs next to HA Core. HA called apps "add-ons" before release 2026.2. |
| Integration | Python code that HA Core loads. It adds entities, actions and configuration screens to HA. |
| Agent definition | The configuration of one agent, owned by the user. |
| Agent state | The data that an agent creates and changes itself: rules, schedules, notes and history. |
| Attachment | An entity or device that the definition links to the agent, as an input, an output or feedback. |
| Invocation | One call to an agent, from an automation, a schedule, an event or a conversation. |
| Agent turn | An invocation that the full LLM pipeline handles: instructions, memory, tools and history. |
| Rule | Logic that an agent writes to answer an invocation without an agent turn. |
| Calculation rule | A rule that computes the answer from input values with a template. |
| Decision model | A model that returns typed answers with probabilities, not text. Examples: the OpenAI Decisions API, and Jev on OpenRouter. |
| Decision rule | A rule that asks a decision model a fixed question. |
| Reducer | The function that converts decision model probabilities into one output value. |
| Output policy | Limits that the runtime applies to every write to an output entity. |
| Scope | The set of HA data and actions that an agent may use. |
| Shadow mode | A rule runs, but its answer is only logged and compared. It does not take effect. |
| Review | A scheduled agent turn in which the agent examines its rules and decisions. |

## 4. Requirements

The owner gave these requirements:

- R1. Automations call an agent explicitly. This is the primary trigger.
- R2. An agent can schedule its own triggers, from timers or from the HA
  event stream.
- R3. The definition attaches devices or entities as inputs and as outputs.
  An output attachment gives the agent permission to set that entity.
- R4. Instead of outputs, an agent can return text or structured output to
  the caller.
- R5. An agent learns through memory, notes and history.
- R6. An agent can write its own rules. A rule answers an invocation without
  an agent turn, by calculation or by a decision model.
- R7. An agent wakes up on a schedule to review the performance of its rules,
  and changes them if necessary.
- R8. The definition lists the tools of the agent: self-modification, memory,
  and HA access.
- R9. HA access has a scope: read-only, specific devices or entities only, or
  full administration, including changes to automations.
- R10. The definition can expose the agent to Assist, so that a person can
  talk to it. Assist is the HA voice and chat assistant. This is optional and
  off by default.
- R11. hagent supports OpenAI and OpenRouter as providers.
- R12. hagent runs as an HA app.
- R13. Cost is a primary design goal.

## 5. Prior art

- **Jarvis-AIO** is a community integration with a related goal. It learns
  routines, suggests automations, and acts alone only after the user accepts
  the same suggestion 3 times. It sends events through a tiered pipeline:
  templates, then a cache, then a classifier model, then a large model. hagent
  takes three ideas from it: tiered handling, trust that the agent earns, and
  local SQLite storage. hagent differs in one main way. Jarvis-AIO is one fixed
  assistant. hagent hosts agents that the user defines.
- **HA AI Task and conversation agents** are one-shot or short-lived. hagent
  can use them as a fallback provider, but does not depend on them.
- **Memory integrations** for Assist (several exist) add long-term notes to
  chat agents. They do not add rules, schedules or output control.

## 6. Architecture

hagent has two parts: an app and an integration.

```
+---------------------------- Home Assistant host ----------------------------+
|                                                                             |
|  HA Core                                   hagent app (container)           |
|  +--------------------------------+        +------------------------------+ |
|  | hagent integration             |  link  | Agent runtime                | |
|  |  - agent definitions (UI)      |<------>|  - router (rules -> LLM)     | |
|  |  - hagent.invoke action        |        |  - rule engine + reducers    | |
|  |  - per-agent entities          |        |  - scheduler + subscriptions | |
|  |  - Assist agents (optional)    |        |  - scope + output policy     | |
|  +--------------------------------+        |  - memory, history (SQLite)  | |
|  | Automations, entities, events  |<-------|  - provider adapters         | |
|  +--------------------------------+  HA    |  - inspector UI (Ingress)    | |
|                                      API   +--------------+---------------+ |
+-----------------------------------------------------------|-----------------+
                                                            | HTTPS
                                                 OpenAI / OpenRouter
```

**The integration** is thin. It does these tasks:

- It stores agent definitions. Each agent is one configuration subentry of
  the integration, so the user edits agents in the normal HA settings screens.
  HA entity selectors let the user pick attachments.
- It provides the `hagent.invoke` action (section 9).
- It creates entities for each agent: status, last result, cost today,
  enabled, and autonomy level.
- It registers an Assist conversation agent for each agent that allows it.
- It sends definitions to the app, and forwards invocations to the app.

**The app** does all agent work. It has these properties:

- It runs as a long-lived process, outside the HA Core event loop. A slow LLM
  call cannot delay HA.
- It continues to run when HA Core restarts.
- It connects to HA Core through the Supervisor proxy, with the
  `SUPERVISOR_TOKEN` that the Supervisor supplies. It uses the HA WebSocket
  API for state, events and service calls.
- It keeps agent state in SQLite under the app data directory. HA backups
  include this directory.
- It serves an inspector UI through Ingress. Ingress shows an app web page
  inside the HA sidebar, behind HA login. The inspector shows rules, notes,
  decisions, costs and reviews for each agent.

**The link** between the integration and the app is a WebSocket connection
on the internal Supervisor network. The integration finds the app by its
host name. The app authenticates the integration with a shared secret that
the app creates on first start.

## 7. Agent definition and agent state

hagent keeps the definition and the state apart. This separation makes scope
enforcement possible.

**Agent definition** (owned by the user, stored in the integration):

| Field | Content |
|---|---|
| Name and instructions | What the agent is for, in plain text. |
| Models | A chat model for agent turns, a model for reviews, and an optional decision model. Each has a provider. |
| Inputs | Entities, devices or areas whose values the agent receives. |
| Outputs | Entities that the agent may set, each with an output policy. |
| Feedback | Entities that report on the quality of decisions (section 14). |
| Result type | `outputs`, `text`, or `structured` with a JSON schema. |
| Tools and scopes | Section 8. |
| Autonomy | `observe`, `suggest` or `act` (section 11). |
| Assist | Off by default. If on, the agent appears as an Assist conversation agent. |
| Budget | A daily cost limit, and limits for self-scheduled triggers. |
| Review schedule | How often the agent reviews itself. The default is daily. |

**Agent state** (owned by the agent, stored in the app):

- Rules, with their lifecycle state and statistics.
- Schedules and event subscriptions.
- Notes.
- History: invocations, decisions, outcomes and feedback.
- Review reports.

An agent can read its definition, but it cannot change it. No tool writes to
the definition. Only the user changes a definition, through the HA UI. An
agent therefore cannot widen its own scope, its outputs or its budget.

## 8. Tools and scopes

The definition turns on tool groups and sets a scope for each group.

**`self`** — tools that change agent state:

- Create, change, promote, demote and retire rules.
- Set, list and cancel timers and cron schedules.
- Subscribe to and unsubscribe from HA events and state changes, with a
  filter.
- Request an early review.

**`memory`** — tools for notes and history:

- Read and replace notes.
- Search history and decisions.
- Read rule and decision statistics.

**`ha`** — tools for Home Assistant, at one of four levels:

| Level | Permitted |
|---|---|
| `none` | Nothing. The agent receives only its inputs. |
| `read` | Read states, history and statistics. The scope limits this to listed entities, devices, areas, labels or domains, or allows all. |
| `control` | `read`, plus service calls on permitted targets. The scope can also list permitted services, for example `climate.set_temperature` but not `climate.turn_off`. |
| `admin` | `control`, plus changes to HA configuration: automations, scripts, helpers, scenes and dashboards. |

Output attachments are a short form of `control`. An agent may always set its
own output entities, through the output policy.

For `admin`, the runtime adds these controls:

- It saves the previous version of each item before a change.
- It writes each change to an audit log.
- If the definition requires it, it asks the user to approve each change
  through an HA notification.

**Enforcement.** Through the Supervisor proxy, the app has administrator
access to HA. HA tokens have no fine-grained scopes. The app runtime therefore
checks every tool call against the scope before it sends anything to HA. The
instructions describe the scope to the model, but the runtime enforces it.
Tests must show that a call outside the scope never reaches HA.

## 9. Invocation

### 9.1 Sources

An invocation comes from one of four sources:

1. An automation calls `hagent.invoke`.
2. A timer or cron schedule that the agent set.
3. An HA event or state change that the agent subscribed to.
4. A person, through Assist, if the definition allows it.

### 9.2 The `hagent.invoke` action

| Field | Meaning |
|---|---|
| `agent` | The agent to call. |
| `data` | Optional extra context, as a mapping. |
| `wait` | `true` (default): return the result as response data. `false`: return at once, and send the result later. |
| `timeout` | Maximum wait in seconds. The default is 30. |

When `wait` is true, the action returns this response data:

```yaml
value: 45            # the main result
result: {...}        # structured result, if the result type is structured
text: "..."          # text result, if the result type is text
path: rule           # rule, decision or agent_turn
rule_id: r12         # if a rule answered
confidence: 0.82     # if a decision model answered
cost_usd: 0.0000     # cost of this invocation
applied: [...]       # output writes, if the agent set outputs
```

When `wait` is false, or when the timeout expires, hagent finishes the work
in the background. It then fires an `hagent_result` event and updates the
last-result entity of the agent.

If the app is not reachable, the action fails with an error. The automation
can catch the error with `continue_on_error`. A connectivity entity shows the
state of the link.

### 9.3 The router

The router handles each invocation in this sequence:

1. It reads the current values of the input attachments.
2. It looks for an active rule that matches the invocation.
3. If a calculation rule matches, the runtime computes the value. No model
   call occurs.
4. If a decision rule matches, the runtime sends one decision model request.
5. If no rule matches, or a rule cannot answer, the runtime starts an agent
   turn.
6. The runtime applies the output policy to every output write.
7. The runtime records the invocation, the path, the result and the cost.

### 9.4 When a rule cannot answer

| Condition | Default action |
|---|---|
| A required input is `unknown` or `unavailable`. | Start an agent turn. The rule can declare "keep the last value" instead. |
| The calculation fails. | Start an agent turn, and record the failure against the rule. |
| Decision confidence is below the rule threshold. | Start an agent turn. |
| The decision model provider fails. | Keep the last value if it is newer than a limit. Otherwise start an agent turn. |
| The daily budget is spent. | Rules continue. Agent turns stop. The status entity shows the reason. |

## 10. Rules

### 10.1 Calculation rules

A calculation rule is an HA template over the input values, with a match
condition. Example: "Is a person home?" computed from presence entities.

The app sends the template to HA Core to render it. HA templates are
familiar to HA users, and HA renders them in a sandbox. A template rule
therefore cannot run arbitrary code.

### 10.2 Decision rules

A decision rule has a fixed shape:

| Part | Content |
|---|---|
| Question | A compact question that the agent writes. |
| Choices | A fixed list: yes/no, categories, or numbered options. |
| Bindings | The input values and the history summary that go into the request. |
| Reducer | The function that converts probabilities to a value. |
| Threshold | The minimum confidence for the answer to take effect. |

hagent supplies three reducers:

- `most_likely`: the choice with the highest probability.
- `expected_value`: the probability-weighted mean of numeric choices. A
  choice can be marked as special, for example "off", and excluded from the
  mean.
- `threshold`: a yes/no result with separate on and off probabilities, for
  hysteresis.

Example: the first heating automation in section 2 is one decision rule. Its
16 temperatures are the choices. Its weighted mean is the `expected_value`
reducer, with "0" as the special "off" choice. Its clamp, step limit and
hysteresis move into the output policy.

### 10.3 Providers for decision rules

The OpenAI Decisions API and the OpenRouter decisions endpoint both return
typed answers with probabilities. hagent puts both behind one adapter
interface. The OpenRouter decisions endpoint is in alpha, so the adapter must
expect changes.

hagent calls the providers directly. It does not use the existing HA
decision or AI Task actions. Direct calls let hagent measure cost per
invocation, use prompt caching, and run when HA Core restarts.

### 10.4 Rule lifecycle

```
draft --> shadow --> active --> retired
            ^           |
            +-----------+  (demoted by review or by failures)
```

- **draft**: the agent wrote the rule. It does not run.
- **shadow**: the rule runs on a sample of invocations. An agent turn also
  runs on the same invocation. The runtime records whether the two answers
  agree.
- **active**: the rule answers invocations.
- **retired**: the rule no longer runs. The runtime keeps it for history.

The runtime promotes a rule from shadow to active only when it meets the
promotion criteria. The defaults are 20 compared invocations and 90 percent
agreement. A review or repeated failures can demote an active rule to
shadow.

### 10.5 Shadow mode and cost

If an agent turn ran beside every shadow invocation, shadow mode would cost
more than no rule at all. The runtime therefore samples shadow comparisons.
The default rate is 1 in 5 invocations, with a daily limit. After promotion,
the runtime continues a small sample, 1 in 50 by default, to detect drift.

Decision models give no reasons for their answers. The review therefore
judges decision rules by outcomes and agreement, not by explanations.

## 11. Outputs

### 11.1 Output policy

The runtime applies an output policy to every write, from rules and from
agent turns. The definition sets the policy for each output entity:

| Setting | Effect |
|---|---|
| Minimum and maximum | Clamp the value. |
| Allowed values | For select-type entities. |
| Maximum step | Limit the change in one write. |
| Minimum interval | Limit how often the entity changes. |
| Deadband | Ignore changes smaller than this amount. |
| Hysteresis | Separate thresholds for "turn on" and "turn off". |
| Verify | Read the state back after the write, and retry if it does not match. |
| Retries | Number of retries before the runtime reports a failure. |

On a final failure, the runtime creates an HA notification and records the
failure in history.

### 11.2 Autonomy

The definition sets one of three autonomy levels:

- `observe`: the agent decides, and the runtime records the decision. It
  writes nothing.
- `suggest`: the runtime sends the decision as an HA notification with
  actions to accept or reject it. Accept and reject are feedback.
- `act`: the runtime applies the decision through the output policy.

A new agent should start at `observe` or `suggest`. The user moves it to
`act` after the decision log shows good results. This follows the Jarvis-AIO
idea of earned trust, but the user makes the change, not the agent.

### 11.3 Text and structured results

If the result type is `text` or `structured`, the agent returns the result
to the caller. It writes no entities. The runtime validates structured
results against the JSON schema in the definition. If validation fails, the
runtime asks the model once to correct the result, and then reports an error.

## 12. Self-scheduling

An agent with the `self` tools can create:

- Timers, for one time in the future.
- Cron schedules.
- Event subscriptions, for HA events or state changes, with a filter.

Each scheduled trigger is an invocation, and it goes through the router like
any other. Rules can answer scheduled invocations too.

The definition sets limits:

- The minimum interval between scheduled invocations. The default is 60
  seconds.
- The maximum number of active schedules and subscriptions. The default is 20.
- A debounce period for each subscription, so that a sensor that changes
  often cannot cause many invocations.

The scheduler keeps schedules in SQLite, and restores them when the app
starts.

## 13. Memory and history

Each agent has three kinds of memory:

- **Notes**: a text document that the agent edits. It holds facts and
  conclusions, for example "the living room takes 40 minutes to warm by
  1 °C." The runtime sends the notes with every agent turn. The definition
  limits the size, 4,000 characters by default.
- **History**: a record of each invocation, its inputs, path, result,
  outputs, cost and later feedback. The agent searches it with the `memory`
  tools.
- **Input history**: the app subscribes to state changes of the input
  attachments, and keeps hourly summaries. An agent turn receives a compact
  summary instead of a fresh database query. This removes the context rebuild
  that the current automations do on every run.

A daily task summarises old history and deletes raw records after a retention
period. The default retention is 30 days.

## 14. Feedback

Feedback tells the agent if its decisions were good. The definition can
attach feedback in three forms:

- **Explicit feedback**: entities that a person sets, for example a comfort
  selector with the values "cold", "OK" and "hot".
- **Implicit feedback**: a person changes an output entity soon after the
  agent set it. The runtime detects this as an override. The window is
  configurable, 30 minutes by default.
- **Suggestion responses**: accept or reject actions on `suggest`
  notifications.

The runtime links each feedback event to the decisions before it, and stores
the link in history.

## 15. Review

The review is a scheduled agent turn. It uses the review model in the
definition. The runtime gives the review model:

- Statistics for each rule: number of answers, shadow agreement, failures,
  overrides and feedback.
- Cost for the period, by path.
- The notes.
- A sample of recent decisions with their outcomes.

The review can create, change, promote, demote or retire rules, change
schedules, and update the notes. It writes a review report. The inspector UI
shows the report.

The review runs daily by default. The runtime also starts a review early if
a rule fails often, or if overrides exceed a limit.

## 16. Assist exposure

If the definition allows it, the integration registers the agent as an
Assist conversation agent. A person can then select the agent in an Assist
pipeline, or talk to it in the HA chat window.

A conversation turn is an agent turn with the same notes, history, tools and
scopes. The runtime keeps the conversation history in the app, so it does
not expire after 5 minutes. Rules do not answer conversation turns.

## 17. Providers

hagent supports two providers:

- **OpenAI**: chat models through the Responses API, and the Decisions API.
- **OpenRouter**: chat models through the OpenAI-compatible API, and the
  decisions endpoint, which provides Jev.

Each provider has one adapter. The adapter reports the token use and cost of
each call. The definition can choose a different provider and model for agent
turns, reviews and decision rules. A low-cost model for agent turns and a
stronger model for reviews is a typical choice.

The user enters API keys in the app configuration. The app never sends keys
to HA, and never writes them to logs.

## 18. Cost controls

- Rules answer repeated invocations without an LLM.
- Decision rules use decision models, which bill input tokens only.
- Input history summaries replace large context rebuilds.
- Shadow comparisons use samples.
- Each agent has a daily budget. When the budget is spent, agent turns stop
  and rules continue.
- Limits on self-scheduled triggers prevent runaway invocations.
- Cost entities in HA show the cost per agent per day, so the user can make
  automations and alerts from them.

## 19. Failure handling

| Failure | Behaviour |
|---|---|
| The app stops. | `hagent.invoke` fails with an error. The connectivity entity turns off. Existing automations can use a fallback. |
| HA Core restarts. | The app reconnects, and restores subscriptions. |
| A provider fails. | The runtime retries with backoff, then follows section 9.4. |
| An output write fails. | The runtime follows the output policy (section 11.1). |
| An input becomes unavailable. | The router follows section 9.4. |

## 20. Security

- The app has administrator access to HA. The scope check in section 8 is the
  only barrier between an agent and full access. It must have complete test
  coverage.
- Agents cannot change their definitions (section 7).
- `admin` changes keep a previous version and an audit log.
- The inspector UI is available only through Ingress, behind HA login.
- API keys stay in the app configuration.

## 21. Worked example: heating

This example shows one agent that uses most of the features.

**Definition**

- Instructions: keep occupants comfortable at the lowest energy cost.
- Inputs: indoor and outdoor temperature, boiler state, presence of each
  occupant, calendars and the weather forecast.
- Outputs: the boiler flow temperature (20–90 °C, maximum step 5 °C, "0"
  means off), and the position of each radiator valve (0–100 percent, verify
  and retry).
- Feedback: one comfort selector for each occupant, plus overrides.
- Tools: `self`, `memory`, and `ha: read` on the home's climate and
  presence entities.
- Autonomy: `suggest` at first, then `act`.
- Models: a low-cost chat model, a stronger review model, and a decision
  model.

**Expected behaviour**

1. An automation calls the agent every 5 minutes.
2. At first, no rules exist. Each call is an agent turn. The agent records
   decisions and writes notes, for example about warm-up times.
3. The agent writes a calculation rule for occupancy, and a decision rule for
   the flow temperature with the `expected_value` reducer.
4. The rules run in shadow mode on a sample. After 20 comparisons with 90
   percent agreement, the runtime promotes them.
5. Most calls are then answered by rules, at low or no cost.
6. When an occupant reports "cold", or changes a valve by hand, the runtime
   records feedback. If this happens often, the runtime starts an early
   review.
7. The daily review checks the results, adjusts the rules and updates the
   notes.

## 22. Out of scope for the first version

- Agents that talk to other agents.
- Access from outside HA, for example through the Model Context Protocol
  (MCP) or the Agent2Agent (A2A) protocol.
- Trained statistical models, for example a learned room presence model.
  Rules and memory come first.
- Support for HA installations without the Supervisor, for example a
  Container install.

## 23. Open questions

Each question has a proposed default.

| # | Question | Proposed default |
|---|---|---|
| 1 | How does the user install the integration? An app cannot install a custom integration through any documented API. | Publish the integration through HACS. As a later option, the app copies it into the configuration folder. |
| 2 | Can the integration discover the app automatically? | Try Supervisor discovery. If it does not work for a custom integration, the user enters the host name once. |
| 3 | Default `hagent.invoke` timeout. | 30 seconds. |
| 4 | Default for `wait`. | `true`. |
| 5 | Shadow sample rate and promotion criteria. | 1 in 5, then 20 comparisons at 90 percent agreement. |
| 6 | Review frequency. | Daily, plus early reviews on failures. |
| 7 | Default daily budget for each agent. | USD 0.50. |
| 8 | Should calculation rules use HA templates, or an expression language inside the app? | HA templates. |
| 9 | Should the app use a separate non-admin HA user for agents below `admin`? The Supervisor proxy always acts as an administrator. | No. Rely on the runtime scope check, and revisit if the risk changes. |
| 10 | Should hagent also accept HA AI Task entities as a provider? | Not in the first version. |
| 11 | Language and framework for the app. | Python 3.12, with the official OpenAI SDK for both providers. |

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
