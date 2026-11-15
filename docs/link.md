# hagent integration link

This document is the contract between the hagent app and the hagent
integration. It describes the link protocol, version 1. Design sections 6,
9.2, 9.3 and 15 set the behaviour that the link carries.

## Terms

- **Integration**: the HA custom integration in
  `integration/custom_components/hagent`. HA Core loads it.
- **App**: the hagent app. It runs in its own container.
- **Link**: one WebSocket connection from the integration to the app.
- **Shared secret**: a random text that the app creates on the first start.
  The integration sends it on each connect.
- **Command**: a message from the integration that asks the app to do
  something. Each command has an integer `id`.
- **Response**: the answer of the app to one command. It has the same `id`.
- **Agent snapshot**: the data of one agent that the integration shows on
  the agent device.
- **Delivered result**: a result that the app sent back as response data to
  a waiting `invoke` command.

## Connection

The integration opens a WebSocket to `ws://<host>:<port>/api/link/ws`. The
default port is 8099.

- The upgrade request must carry `Authorization: Bearer <secret>`.
- If the secret is wrong or missing, the app answers HTTP 401 with the
  error code `unauthorized`. The app does not open the WebSocket.
- The app compares the secret in constant time.
- The Ingress guard does not check the link path, because HA Core connects
  from its own address (design section 6.1). The exemption is for the exact
  path only. The guard checks every other path, also `/api/link`,
  `/api/link/ws2` and each subpath such as `/api/link/ws/x`.
- The guard compares the raw path, before percent-decoding, because the
  router also matches the raw path. A path with an encoded character, for
  example `/api%2Flink%2Fws` or `/%61pi/link/ws`, is not exempt. A path
  with an encoded separator (`%2F` or `%5C`) is never exempt.
- Each frame holds one JSON object. The largest frame is 16 MiB.
- Both sides send WebSocket pings every 30 seconds.

The app keeps the secret in the file `link_secret` in its data directory.
Only the owner can read the file (mode 0600). The integration gets the
secret in one of two ways:

1. Supervisor discovery. When the app runs in a container, it posts its
   host, port and secret to the Supervisor (`POST /discovery`, service
   `hagent`). HA then offers the set-up, and the user confirms it. The app
   configuration must list `hagent` under `discovery`. A discovery never
   changes an existing config entry. Another app can send a discovery
   with its own host and secret, so the integration stops the flow with
   `already_configured` and keeps the existing values.
2. By hand. The user reads the values from `GET api/integration` in the
   management API (see `docs/api.md`) and enters them in the config flow.

## Start of a connection

After the upgrade, the app sends one `hello` message:

```json
{"type": "hello", "protocol": 1, "installation_id": "<ID>",
 "agents": [<agent snapshot>, ...]}
```

- `protocol` is the protocol version. The integration closes the link if
  the version is not 1.
- `installation_id` is the ID of the app installation. The config flow uses
  it as the unique ID of the config entry.
- `agents` holds every live agent.

The `hello` list is complete. The integration removes the device of each
agent that is not in the list. This step also removes an agent that the
user deleted while the link was down.

## Agent snapshot

| Field | Content |
|---|---|
| `id` | The agent ID. |
| `name` | The agent name. |
| `enabled` | True if the agent is on. |
| `version` | The current definition version. |
| `assist` | True if the definition allows Assist. Only then does the integration add a conversation entity. |
| `has_outputs` | True if the definition has outputs. |
| `result_type` | `none`, `text` or `structured`. |
| `status` | `idle`, `running`, `budget_spent`, `off` or `error`. |
| `running` | The number of invocations of the agent that run now. |
| `cost_today_usd` | The spend of the agent today, or `null` if the app does not know it. |
| `last_result` | The last finished invocation, or `null`. |

The app sets `status` in this order:

1. `off`: the agent is turned off.
2. `running`: one or more invocations run.
3. `budget_spent`: the daily budget of the agent has no money left.
4. `error`: the last invocation ended with status `error`.
5. `idle`: none of the above.

`last_result` has these fields: `invocation_id`, `status`, `path`,
`result`, `finished_at`, and `error` (`code`, `message`) for a failed
invocation. If the JSON text of `result` has more than 16000 characters,
`result` is `null` and `result_clipped` is true.

## Messages from the app

| Type | When | Fields |
|---|---|---|
| `hello` | Once, after the upgrade. | See above. |
| `agent` | An agent is created or changed, an invocation starts or ends, or the periodic refresh runs (every 300 seconds by default). | `agent`: the agent snapshot. |
| `agent_removed` | The user deleted the agent. | `agent_id`. |
| `result` | Each invocation ends, also one that no command waits for. | `delivered`, `result`. |
| `response` | The answer to one command. | See "Responses". |

`result.result` holds the response data of design section 9.2 and the
field `agent_id`. If `delivered` is false, the integration fires the HA
event `hagent_result` with `result.result` as the event data. If
`delivered` is true, the caller already has the result, and the
integration does nothing.

The app can send the `result` message of an invocation before the
`response` of the same `invoke` command.

## Commands

Each command has `type` and an integer `id`. The integration chooses the
`id`. The app runs the commands at the same time, so the responses can
arrive in another order.

### invoke

```json
{"type": "invoke", "id": 1, "agent_id": "<ID>", "source": "invoke",
 "text": null, "data": {}, "conversation_id": null,
 "result_schema": null, "attachments": [], "caller": {},
 "wait": true, "timeout": 30, "on_timeout": null}
```

| Field | Content |
|---|---|
| `agent_id` | Required. The agent. |
| `source` | `invoke` (default), `ai_task`, `ai_task_evaluate` or `assist`. |
| `text` | Optional text. The agent receives it as `text` in the caller data. |
| `data` | Optional caller data, as an object. |
| `conversation_id` | Required for `assist`. Each conversation has its own queue. |
| `result_schema` | Optional JSON schema with `"type": "object"`. It replaces the result type of the definition for this invocation. |
| `attachments` | Optional list of `{"name", "mime_type", "data"}`. `data` is base64. |
| `caller` | Optional metadata of the caller, for history only. The app does not check it. |
| `wait` | `true` (default): answer with the result. `false`: answer at once. |
| `timeout` | The longest wait in seconds, from 0 to 3600. The default is 30. |
| `on_timeout` | `cancel` or `continue`. The default is `cancel` for agents with outputs and `continue` for other agents. |

Attachment rules:

- The app accepts these types: `image/png`, `image/jpeg`, `image/gif`,
  `image/webp`, `application/pdf`, `text/plain`, `text/markdown`,
  `text/csv` and `application/json`.
- The sum of the sizes of one invocation must not exceed 8 MiB. The
  integration reads the file size before it reads a file. It reads only
  regular files.
- The model receives images and PDF files as binary parts. It receives
  text files as text, marked as data, not instructions.
- The caller data gets `attachments`: the name, the type and the size of
  each file.

The response data:

- `wait` is false: `{"invocation_id": "<ID>", "status": "queued"}`. If the
  router refuses the invocation at once (for example `agent_off`), the
  data is the result instead.
- The invocation ends in time: the response data of design section 9.2:
  `invocation_id`, `status`, `result`, `writes`, `path`, `cost_usd`, and
  `rule_id`, `rule_version`, `fallback_reason` and `error` when they apply.
- The timeout expires with `continue`:
  `{"invocation_id": "<ID>", "status": "running", "timed_out": true}`. The
  result comes later in a `result` message with `delivered` false.
- The timeout expires with `cancel`: an error response with the code
  `timeout`. The app cancels the invocation. Its `result` message has
  `delivered` true.

An `invoke` with the source `assist` for an agent whose definition does not
allow Assist gets the router code `assist_off`. The integration adds a
conversation entity only for agents that allow Assist. The app checks the
definition too, because the app owns it.

A response with `success` true can still hold `status` `error`, for
example with the router codes `agent_not_found`, `agent_off`,
`assist_off`, `busy` or `queue_full`. The integration turns such a result into an HA error.

### cancel

```json
{"type": "cancel", "id": 2, "invocation_id": "<ID>"}
```

The app cancels a queued or running invocation. The response data is `{}`.
The error code `not_found` means that no queued or running invocation has
this ID.

### set_enabled

```json
{"type": "set_enabled", "id": 3, "agent_id": "<ID>", "enabled": false}
```

The app turns the agent on or off. The response data is
`{"enabled": false}`. The app then sends an `agent` message.

### ping

`{"type": "ping", "id": 4}`. The response data is `{}`.

## Responses

```json
{"type": "response", "id": 1, "success": true, "data": {...}}
{"type": "response", "id": 1, "success": false,
 "error": {"code": "<code>", "message": "<text>"}}
```

Error codes of the link:

| Code | Meaning |
|---|---|
| `invalid_message` | The command is not valid. A frame that is not JSON, or has no integer `id`, gets this code with `id` null. |
| `unknown_command` | The `type` is not known. |
| `timeout` | An `invoke` command timed out with `on_timeout: cancel`. |
| `not_found` | `cancel` found no invocation. |
| `agent_not_found` | `set_enabled` found no agent. |
| `not_running` | The router is not running, for example while the app stops. |
| `internal_error` | An unexpected error. The app log has the details. |

## Behaviour of the integration

- The integration loads its config entry also when the app is down. The
  connectivity sensor on the hagent device is then off.
- After a failure, the integration connects again with back-off: 1 second
  at first, then twice as long each time, at most 60 seconds.
- When the link drops, each command that waits for a response fails at
  once. The agent entities become unavailable. `hagent.invoke`, AI Task
  calls and Assist messages fail with an error until the link is open
  again.
- The integration waits for an `invoke` response for `timeout` plus 10
  seconds. The app answers first with its own timeout result.
- HA limits a state to 255 characters. The last-result sensor cuts its
  state. Its attributes hold the full `last_result` object.

## HA features

| HA feature | Link use |
|---|---|
| `hagent.invoke` action | `invoke` with the source `invoke`. The action targets one agent device, or one entity of an agent device. |
| AI Task `GENERATE_DATA` | `invoke` with the source `ai_task`. `text` is the task instructions. `data.task_name` is the task name. A task structure becomes `result_schema`. |
| AI Task `SUPPORT_ATTACHMENTS` | The integration reads the attachment files and sends them in `attachments`. |
| AI Task `EVALUATE` | `invoke` with the source `ai_task_evaluate`. `data` holds `task_name`, `questions` and `state`. `result_schema` asks for one answer for each question. HA Core 2026.11 adds this feature. On an older HA Core, the entity does not offer it. |
| Conversation entity | `invoke` with the source `assist` and the HA conversation ID. |
| Enabled switch | `set_enabled`. |

## Who can call an agent

An agent can have more rights in HA than the HA user who calls it. An agent
with the `admin` level or a wide scope can call admin actions such as
`homeassistant.restart`. So the integration applies the rule of HA admin
actions to each call that reaches the link:

- A call without an HA user can invoke an agent. Automations and scripts
  that no user starts make such calls. A voice satellite also makes such
  calls when its pipeline runs without a user.
- A call by an admin user can invoke an agent.
- A call by any other user fails with `Unauthorized`. The integration does
  not send an `invoke` command to the app.

The rule applies to these HA features:

| HA feature | How the integration applies the rule |
|---|---|
| `hagent.invoke` action | HA registers it as an admin action (`async_register_admin_service`). HA itself refuses the call. |
| AI Task entity | The `ai_task.*` actions belong to HA Core, so the integration cannot make them admin actions. The entity checks the user of the call context before it sends the task. |
| Conversation entity | The entity checks the user of the message context. For a user who is not an admin, Assist shows an error. |

A script inherits the context of the user who starts it. Thus a script
that a non-admin user starts also fails when it calls an agent. A
non-admin user cannot use Assist with a hagent agent.

In the prompt, the app shows `text` on its own line as the request. It
marks the other caller data as data, not instructions (design section 19).
This mark does not replace the rule above: a model can still follow text
in the data.

## Seam for approval notifications

Task-16 sends approval notifications from the app, through
`MobileAppNotifier`. The app cannot read the HA user or the webhook ID of a
phone registration. A later version of the link can add a command from the
app to the integration, for example `notify`, so that HA Core sends the
notification to the registrations of an HA user. The app then sets
`approvals.notifier` to a sender that uses this command. Version 1 has no
command from the app to the integration.

## Version changes

A change that an old integration cannot read needs a new protocol number.
New optional fields and new message types do not need one. The integration
ignores message types that it does not know.
