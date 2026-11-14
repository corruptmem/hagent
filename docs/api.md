# hagent management API

This document is the contract between the hagent app and the management UI.
It describes the JSON API and the change-event WebSocket. Design section 22
sets the UI stack. Design section 7 sets the data that the API manages.

## Terms

- **Management API**: the JSON API that the app serves for the management
  UI. Only the UI uses it.
- **Agent definition**: the configuration of one agent that the user owns
  (design section 7.1).
- **Version**: one saved state of an agent definition. A version never
  changes. Each change to a definition adds a new version (design section
  10.6).
- **Current version**: the newest version of an agent. The runtime uses it
  for new invocations.
- **Private store**: the knowledge store that hagent creates with each agent
  (design section 13.4).
- **Live agent**: an agent that the user did not delete.
- **Change event**: a short message that says that a record changed.

## General rules

- The app serves the API on its HTTP port: 8099, or the port in the app
  options. Ingress forwards requests from
  `/api/hassio_ingress/<token>/` to the app. The UI must use relative URLs,
  for example `api/agents`, not `/api/agents`.
- The app accepts connections only from the Supervisor address,
  `172.30.32.2` (design section 6.1). It refuses other addresses with 403
  `forbidden`. It uses the address of the TCP peer, not a header. Before the
  app is ready, it refuses all requests with 503 `not_ready`. See "Ingress
  options".
- The API does not ask for a login. HA authenticates the user before
  Ingress forwards a request (design section 6.1).
- Request and response bodies are JSON objects, with the content type
  `application/json`.
- IDs are 32 lower-case hexadecimal characters.
- Times are ISO 8601 text in UTC, for example
  `2026-11-14T23:03:00.123456+00:00`.
- A field name in this document uses dots for nested fields. For example,
  `tools.ha.level` is the field `level` in the object `ha` in the object
  `tools`.

## Errors

Each error response has this body:

```json
{"error": {"code": "version_conflict", "message": "...", "details": {}}}
```

- `code` is a stable identifier. The UI uses it to select a behaviour.
- `message` is English text for a person.
- `details` is optional. Its content depends on the code.

| Status | Code | Cause |
|---|---|---|
| 400 | `invalid_json` | The body is not JSON. |
| 400 | `invalid_body` | The body is JSON, but a field is missing or has the wrong type. |
| 400 | `invalid_query` | A query parameter is missing or is not valid. |
| 400 | `private_store_choice_required` | A delete request did not say what to do with the private store. `details.choices` lists the values. |
| 403 | `forbidden` | The connection does not come from an allowed address. |
| 404 | `not_found` | The path does not exist. |
| 404 | `agent_not_found` | The agent does not exist, or the user deleted it. |
| 404 | `version_not_found` | The agent has no version with this number. |
| 405 | `method_not_allowed` | The path does not accept this method. |
| 409 | `name_conflict` | Another live agent has the same name. Names are not case-sensitive. |
| 409 | `version_conflict` | The `base_version` is not the current version. `details.current_version` gives the current version. |
| 409 | `private_store_in_use` | Other agents attach the private store, so the app cannot delete it. `details.agent_ids` lists them. |
| 422 | `definition_invalid` | The definition is not valid. `details` lists the errors. |
| 503 | `not_ready` | The app is starting or stopping. Try again later. |

For `definition_invalid`, each item of `details` has these fields:

| Field | Content |
|---|---|
| `loc` | The path of the field, as a list. For example, `["tools", "ha", "level"]`. An empty list means the whole definition. |
| `msg` | English text for a person. |
| `type` | A stable identifier of the error kind. |

The app checks the knowledge stores of a definition against its database.
These checks use the `type` values `store_not_found`,
`private_store_missing` and `private_store_access`.

## Agent definition

The definition holds the fields of design section 7.1. The app rejects
unknown fields. The types are strict: for example, the app rejects the text
`"0.5"` where it expects a number.

`GET api/agents/schema` returns the JSON schema of the definition. The UI can
build its forms from it. `GET api/agents/defaults` returns a definition with
every default value.

| Field | Type | Default | Rules |
|---|---|---|---|
| `name` | text | (needed) | 1 to 64 characters. Unique among live agents. |
| `instructions` | text | (needed) | 1 to 100,000 characters. |
| `models.chat` | model | (needed) | |
| `models.decision` | model or `null` | `null` | |
| `models.summary` | model or `null` | `null` | `null` means: use `models.chat`. |
| `inputs.entities` | list of entity IDs | `[]` | |
| `inputs.devices` | list of device IDs | `[]` | |
| `outputs` | list of entity IDs | `[]` | |
| `result.type` | `none`, `text` or `structured` | `text` | |
| `result.schema` | object or `null` | `null` | Needed for `structured`, with `"type": "object"`. Not permitted for the other types. |
| `tools.self` | true or false | `false` | |
| `tools.knowledge` | true or false | `true` | |
| `tools.memory` | true or false | `true` | |
| `tools.ha.level` | `none`, `read`, `control` or `admin` | `none` | |
| `tools.ha.scope` | scope | empty | See "HA scope". |
| `tools.ha.services` | list of service names | `[]` | Only for `control`. |
| `knowledge` | list of attachments | the private store | See "Knowledge attachments". At most 100. |
| `retrieval.mode` | `off`, `assist` or `all` | `off` | |
| `retrieval.result_count` | integer | 5 | 1 to 50. |
| `retrieval.token_limit` | integer | 1000 | 100 to 20,000. |
| `approval.tools` | list of tool references | `[]` | See "Approval". |
| `approval.services` | list of service names | `[]` | See "Approval". |
| `approval.entities` | list of entity IDs | `[]` | See "Approval". |
| `assist.enabled` | true or false | `false` | |
| `budget.daily_usd` | number | 0.50 | 0 to 1000. |
| `budget.max_tool_calls` | integer | 20 | 1 to 500. Limit for one agent turn. |
| `budget.max_tokens` | integer | 200,000 | 1000 to 10,000,000. Limit for one agent turn. |
| `concurrency.mode` | `queued`, `single`, `restart` or `parallel` | `queued` | `parallel` only for an agent with no outputs. |
| `concurrency.queue_size` | integer | 10 | 1 to 100. |
| `fallback.mode` | `last_result`, `fixed`, `no_change` or `error` | `last_result` | |
| `fallback.value` | any JSON value | `null` | Needed for `fixed`. Not permitted for the other modes. |
| `scheduling.min_interval_seconds` | integer | 60 | 1 to 86,400. |
| `scheduling.max_schedules` | integer | 20 | 0 to 1000. Schedules and subscriptions together. |
| `scheduling.debounce_seconds` | number | 5 | 0 to 3600. The default for each subscription. |
| `review.enabled` | true or false | `true` | |
| `review.cron` | cron text | `0 4 * * *` | Five fields. The default is daily at 04:00. |

Formats:

- A **model** is `{"provider": "openai" | "openrouter", "model": "<name>"}`.
- An **entity ID** is `<domain>.<object ID>`, in lower-case letters, digits
  and underscores. For example, `climate.<room>`.
- A **service name** is `<domain>.<service>`. The service can be `*` for
  all services of the domain.
- A list must not contain the same item twice. A list holds at most 500
  items.

The design gives no default for `budget.max_tokens` and
`scheduling.debounce_seconds`. Task-7 chose the defaults above.

### HA scope

`tools.ha.scope` has these fields: `all` (true or false), `entities`,
`devices`, `areas`, `labels` and `domains` (lists).

- For `read` and `control`, the scope must set `all` to true, or list at
  least one target.
- A scope with `all` set to true must have empty lists.
- For `none` and `admin`, the scope must be empty, and `services` must be
  empty. `admin` has no narrower form (design section 8).
- Indirect services must be named one by one. A wildcard such as `script.*`
  is not permitted. The indirect domains are `script`, `automation`,
  `scene`, `homeassistant`, `shell_command`, `rest_command`,
  `python_script`, `pyscript`, `hassio` and `notify`.

The app does not check that HA entities, devices, areas or labels exist. HA
can be offline, and entities change. The tool executor resolves the scope
at run time.

### Knowledge attachments

Each attachment is
`{"store_id": "<ID>", "access": "read" | "read_write", "inject_main": true | false}`.
The defaults are `read` and `false`.

- The store must exist.
- A store can be attached only once.
- The private store of the agent must stay attached, with `read_write`
  access.
- On create, the app adds the private store as the first attachment, with
  `read_write` access and `inject_main` set to true. The request does not
  need to name it.

### Approval

- `approval.tools` holds tool references: a group (`ha`), a tool
  (`ha.call_service`) or all tools of a group (`ha.*`). The groups are
  `self`, `knowledge`, `memory` and `ha`. The group must be on in `tools`.
- `approval.services` and `approval.entities` need `tools.ha.level` set to
  `control` or `admin`, or at least one output.

## Agent object

The API returns an agent as this object:

```json
{
  "id": "<agent ID>",
  "name": "Heating",
  "enabled": true,
  "version": 3,
  "created_at": "<time>",
  "updated_at": "<time>",
  "private_store": {"id": "<store ID>", "name": "heating_private"},
  "definition": {"name": "Heating", "...": "..."}
}
```

- `version` is the current version.
- `enabled` is not part of the definition. A change to it adds no version.
- `private_store` is `null` only if the store is missing.

A list returns the same object without `definition`.

## Endpoints

All paths are relative to the Ingress base path.

### List agents

`GET api/agents`

Returns `{"agents": [<agent without definition>, ...]}`, sorted by name.

### Create an agent

`POST api/agents`

Body: `{"definition": <definition>}`.

The app creates the agent, its private store and version 1. It returns the
agent with status 201. The private store gets the name
`<agent name as identifier>_private`, with a number suffix if the name is
in use.

### Read an agent

`GET api/agents/{id}`

Returns the agent.

### Edit an agent

`PUT api/agents/{id}`

Body: `{"definition": <definition>, "base_version": <integer>}`.

- The body holds the full definition, not only the changed fields.
- `base_version` is optional. Send the version that the user edited. If it
  is not the current version, the app returns `version_conflict` and saves
  nothing. The UI then loads the agent again.
- If the definition is equal to the current definition, the app adds no
  version and returns the agent.
- Otherwise the app adds a version and returns the agent.

### Turn an agent on or off

`PUT api/agents/{id}/enabled`

Body: `{"enabled": true | false}`. Returns the agent. The app adds no version.

### Copy an agent

`POST api/agents/{id}/copy`

Body: `{"name": "<name>"}`, or no body. Without a name, the app uses
`<name> (copy)`, with a number suffix if the name is in use.

The copy starts at version 1, with the current definition of the source.
The copy gets its own new private store. Its private-store attachment points
to the new store, with the same `inject_main` value. The other attachments
stay. The app returns the copy with status 201.

### Delete an agent

`DELETE api/agents/{id}?private_store=keep|delete`

The `private_store` parameter is needed. Without it, the app returns
`private_store_choice_required` and deletes nothing. The UI asks the user,
then sends the request again.

- `keep`: the store stays as a normal shared store. Its `private` flag is
  cleared and it has no owner.
- `delete`: the app deletes the store. If other live agents attach it, the
  app returns `private_store_in_use` and deletes nothing.

Returns `{"deleted": true, "private_store": "keep" | "delete"}`.

The app keeps the versions of a deleted agent, because history records
refer to them. The name of a deleted agent is free again.

### List versions

`GET api/agents/{id}/versions`

Returns `{"versions": [<version without definition>, ...]}`, newest first.
A version object has these fields:

| Field | Content |
|---|---|
| `version` | The version number. Version 1 is the first. |
| `change` | `create`, `edit`, `copy` or `restore`. |
| `source_agent_id` | For `copy`: the source agent. For `restore`: the same agent. Otherwise `null`. |
| `source_version` | For `copy`: the version of the source agent. For `restore`: the restored version. Otherwise `null`. |
| `created_at` | The time of the change. |
| `definition` | The definition. Only in "Read a version". |

### Read a version

`GET api/agents/{id}/versions/{version}`

Returns the version with its definition.

### Compare two versions

`GET api/agents/{id}/diff?from=<version>&to=<version>`

Returns this object:

```json
{"from": 1, "to": 2, "changes": [
  {"path": "budget.daily_usd", "old": 0.5, "new": 2},
  {"path": "outputs", "old": [], "new": ["climate.<room>"]}
]}
```

- `changes` is sorted by `path`.
- A list is one value. The app does not compare list items one by one.

### Restore a version

`POST api/agents/{id}/versions/{version}/restore`

Body: `{"base_version": <integer>}`, or no body.

The app adds a new version with the definition of the earlier version. It
does not remove the versions after it. The app validates the old definition
again, because a store can be gone. If the old definition is equal to the
current definition, the app adds no version. `base_version` works as in
"Edit an agent". Returns the agent.

### Definition schema and defaults

- `GET api/agents/schema` returns the JSON schema of a definition.
- `GET api/agents/defaults` returns `{"definition": <definition>}` with
  every default value, for a new-agent form.

### Test invocation (planned)

`POST api/agents/{id}/test`

The agent runtime (task-10 and task-12) does not exist yet. Until it
exists, the app does not serve this path and returns 404 `not_found`. The UI
shows the test panel and reports that the runtime is not available. This
section is the contract for the runtime task.

A **test invocation** runs one agent turn with sample data. It makes no
change to HA, to knowledge stores or to agent state.

Body:

```json
{
  "definition": {"name": "Heating", "...": "..."},
  "input": {"text": "Is the room warm enough?", "data": {"reason": "test"}},
  "states": {"sensor.<room>_temperature": {"state": "19.5", "attributes": {}}}
}
```

| Field | Content |
|---|---|
| `definition` | Optional. A definition to test before the user saves it. Without it, the app uses the current version. The app validates it as in "Edit an agent". |
| `input.text` | Optional text for the agent, as in `hagent.invoke`. |
| `input.data` | Optional JSON object for the agent, as in `hagent.invoke`. |
| `states` | Optional sample states. Each key is an entity ID. The value has `state` and `attributes`. The agent reads these values in place of the state cache. |

Rules for the runtime:

- The tool executor checks the scope of each call as usual.
- The executor does not send a service call to HA. It records the call and
  returns a success result to the agent.
- Writes to stores, memory, rules and schedules go to a copy that the app
  discards after the turn.
- The turn counts against the budget of the agent. The response shows the
  cost.

Response (status 200):

```json
{
  "result": "The room is warm enough.",
  "planned_calls": [
    {"domain": "climate", "service": "set_temperature",
     "target": {"entity_id": ["climate.<room>"]}, "data": {"temperature": 20}}
  ],
  "denied_calls": [],
  "tool_calls": 3,
  "tokens": 1200,
  "cost_usd": 0.0012
}
```

- `result` matches `result.type` of the definition.
- `planned_calls` lists the service calls that a real turn would send.
- `denied_calls` lists the calls that the scope check refused, each with
  the fields of a planned call and a `reason`.

Errors: `agent_not_found`, `definition_invalid`, and 503 `not_ready` when
the runtime is not running.

## HA catalogue

`GET api/ha/catalog`

Returns the HA entities, devices, areas, floors, labels, domains and
services, for the pickers of the UI. The endpoint is read-only. The app
sends only these fixed commands to HA: `config/entity_registry/list`,
`config/device_registry/list`, `config/area_registry/list`,
`config/floor_registry/list`, `config/label_registry/list` and
`get_services`. The app keeps their results for 30 seconds. The entity
states come from the state cache.

This endpoint is not an agent tool. Only the UI uses it.

```json
{
  "stale": false,
  "entities": [
    {"entity_id": "light.<room>", "name": "<name>", "domain": "light",
     "state": "on", "device_id": "<device ID>", "area_id": "<area ID>",
     "labels": ["<label ID>"], "disabled": false}
  ],
  "devices": [
    {"id": "<device ID>", "name": "<name>", "area_id": "<area ID>",
     "labels": []}
  ],
  "areas": [{"id": "<area ID>", "name": "<name>", "floor_id": "<floor ID>"}],
  "floors": [{"id": "<floor ID>", "name": "<name>"}],
  "labels": [{"id": "<label ID>", "name": "<name>"}],
  "domains": ["light"],
  "services": ["light.turn_off", "light.turn_on"]
}
```

- `entities` holds each entity in the state cache, and each entity in the
  entity registry that has no state, for example a disabled entity. Such
  an entity has `state` set to `null`.
- `name` is the registry name, then the `friendly_name` attribute, then the
  original name, then the entity ID.
- `area_id` is the area of the entity, or else the area of its device.
- `stale` is true when the state cache may not match HA, or when a registry
  command failed. When HA is offline, the app returns the last data that it
  has. A list is empty if the app never received it.

## UI files

`GET {tail}`: each path that does not start with `api/` is a UI path.

- A file of the UI build: the app sends the file.
- A path with a file extension and no file: 404 `not_found`.
- Any other path: `index.html`, so that a deep link opens its screen.

The app adds `<base href="<X-Ingress-Path>/">` to `index.html`. The UI
router reads its base path from this element, and the UI resolves its
relative API URLs against it. The app accepts only a plain path of letters,
digits and `._~-` in `X-Ingress-Path`. It uses `/` for any other value.

### Ingress options

The `ingress` section of the app options has these keys:

| Key | Default | Content |
|---|---|---|
| `allowed_addresses` | `["172.30.32.2"]` | The addresses and CIDR networks that can connect. Change it only for local development, for example to `["127.0.0.1"]`. |
| `ui_dir` | `""` | The directory of the UI build. An empty value selects `frontend/dist` in the source checkout. |

## Change events

`GET api/events` opens a WebSocket. The app sends one JSON object for each
change event. The app ignores messages from the client.

The first message is `{"topic": "events", "action": "ready", "id": ""}`.
Each later message has this form:

```json
{"topic": "agents", "action": "updated", "id": "<agent ID>",
 "data": {"version": 2, "enabled": true}}
```

| Topic | Action | Data |
|---|---|---|
| `agents` | `created` | `version`, `enabled` |
| `agents` | `updated` | `version`, `enabled`. The app sends it for an edit, a restore and a change to `enabled`. |
| `agents` | `deleted` | `private_store`: `keep` or `delete` |

Rules for the UI:

- An event says only what changed. Load the record again through the API.
- After a reconnect, load all data again. The app does not repeat the events
  that the UI missed.
- If a client reads too slowly, the app closes its connection. The client
  then reconnects.

Later tasks add topics, for example for knowledge stores and rules. They
add their topics to this table.
