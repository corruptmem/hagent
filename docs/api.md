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
  options". One path is exempt: the integration link, `api/link/ws`. HA
  Core connects to it directly, and the link checks a shared secret
  instead (see "Integration link").
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
| `budget.daily_usd` | number or `null` | `null` | 0 to 1000. The daily budget of the agent in USD. `null` uses the default agent budget of the settings (see "Settings"). |
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
`devices`, `areas`, `floors`, `labels` and `domains` (lists).

- For `read` and `control`, the scope must set `all` to true, or list at
  least one target.
- A scope with `all` set to true must have empty lists.
- For `none` and `admin`, the scope must be empty, and `services` must be
  empty. `admin` has no narrower form (design section 8).
- Indirect services must be named one by one. A wildcard such as `script.*`
  is not permitted. The indirect domains are `script`, `automation`,
  `scene`, `homeassistant`, `shell_command`, `rest_command`,
  `python_script`, `pyscript`, `hassio`, `notify`, `recorder`, `zwave_js`,
  `zha`, `matter`, `knx`, `mqtt`, `group`, `logger`, `system_log`, `backup`
  and `hagent`.

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
  {"path": "budget.daily_usd", "old": null, "new": 2},
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
  every default value, for a new-agent form. `budget.daily_usd` is the
  current default agent budget of the settings.

### Test invocation

`POST api/agents/{id}/test`

A **test invocation** runs one agent turn with sample data. It makes no
change to HA, to knowledge stores or to agent state. The router (task-12)
runs it outside the queue of the agent. A test therefore never waits for
real work, and never cancels real work in `restart` mode. The app also
tests an agent that is turned off.

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
| `definition` | Optional. A definition to test before the user saves it. Without it, the app uses the current version. The app validates it as in "Edit an agent", with the knowledge-store checks. It does not save it. |
| `input.text` | Optional text for the agent. The agent receives it as `text` in the caller data. |
| `input.data` | Optional JSON object for the agent, as in `hagent.invoke`. |
| `states` | Optional sample states. Each key is an entity ID. The value has `state` (text) and `attributes` (object). The agent reads these values in place of the state cache: in its inputs and in its HA read tools. Each sample entity also goes into the inputs. |

Rules for the runtime:

- The tool executor checks the scope of each call as usual.
- The executor does not send a service call to HA. It records the call in
  the audit log with status `dry_run` and returns a success result to the
  agent.
- Tools that write to stores, memory, rules and schedules check their
  arguments, but do not write.
- The turn counts against the budget of the agent. The response shows the
  cost.
- Rules do not answer a test invocation.
- The invocation history keeps the test with `dry_run` set. A test result
  is never a `last_result` fallback.

Response (status 200):

```json
{
  "invocation_id": "<32 hex>",
  "status": "ok",
  "path": "agent_turn",
  "result": "The room is warm enough.",
  "planned_calls": [
    {"domain": "climate", "service": "set_temperature",
     "target": {"entity_id": ["climate.<room>"]}, "data": {"temperature": 20},
     "return_response": false, "needs_approval": false}
  ],
  "denied_calls": [],
  "tool_calls": 3,
  "tokens": 1200,
  "cost_usd": 0.0012
}
```

- `result` matches `result.type` of the definition.
- `status` and `path` are as in the invocation result (design section
  9.2): `ok` with `agent_turn`, or `fallback` or `error` with `fallback`.
  For `fallback` and `error`, `fallback_reason` says why. For `error`,
  `error` has `code` and `message`.
- `planned_calls` lists the service calls that a real turn would send,
  after the scope check. `needs_approval` is true if a real call would
  wait for approval. A planned admin command has a `tool` field.
- `denied_calls` lists the tool calls that failed, each with the tool
  arguments, `tool`, `code` (the error code of the executor) and
  `reason`.

Errors: `agent_not_found`, `definition_invalid`, `invalid_body`, and 503
`not_ready` when the runtime is not running.

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

### HA users

`GET api/ha/users` lists the HA users for the approver picker of the
settings:

```json
{"source": "auth", "reason": null,
 "users": [
   {"id": "<HA user ID>", "name": "User A", "username": "a_user",
    "is_admin": true}
 ]}
```

- The app sends the read command `config/auth/list` to HA. HA accepts it
  only from an administrator token. The list has the active users that HA
  did not create for itself. It holds no credentials or groups.
- If HA refuses the command or is offline, the app lists the users that
  `person` entities link to (`source` is `persons`). `username` and
  `is_admin` are then `null`, and `reason` says why.
- With no source, `source` is `null` and `users` is empty. The route does
  not fail because of HA.

## Knowledge stores

These routes manage knowledge stores, their files and their documents
(design section 13.4). Task-20 builds the UI screens for them.

Terms:

- **Store**: a named collection of text. Each agent has a private store.
  The user creates shared stores. An agent definition attaches stores in
  `knowledge` (see "Knowledge attachments").
- **File**: Markdown text that agents and the user write. `MAIN.md` is the
  main file of a store.
- **Document**: a file that the user uploads. The app extracts its text,
  splits it into passages and indexes it. Agents cannot change documents.
- **Item**: a file or a document. Files and documents share one path
  namespace in a store.
- **Path**: the name of an item. A path uses letters, digits, `_`, `-`,
  `.`, space and `/`. It has no empty, `.` or `..` parts. Paths do not
  depend on case. In a URL, encode each part of the path.
- **Item version**: a number that grows by one on each change of an item,
  also on a delete. A write names the version that the writer read
  (`base_version`). If the item changed since, the write fails with 409
  `version_conflict`.
- **Size**: the UTF-8 bytes of the text of all items in a store. The size
  limit of a store is 1000 bytes to 1000000000 bytes. The default is
  1000000 bytes. The option `knowledge.default_size_limit` changes the
  default for stores that this API creates. A private store that the app
  creates with an agent starts with 1000000 bytes. The user can change the
  limit of each store.

Errors of all knowledge routes: 404 `not_found`, 503 `not_ready`.

### Store object

```json
{
  "id": "<store ID>", "name": "heating",
  "description": "Facts about the heating system.",
  "private": false, "owner_agent_id": null,
  "size_limit": 1000000, "size": 5120, "files": 3, "documents": 1,
  "created_at": "<time>", "updated_at": "<time>",
  "attached": [
    {"agent_id": "<agent ID>", "agent_name": "Heating",
     "access": "read_write", "inject_main": false}
  ]
}
```

`attached` lists each live agent whose current definition attaches the
store. A store with `private` true is the private store of
`owner_agent_id`.

### List stores

`GET api/knowledge/stores` returns `{"stores": [<store>, ...]}`, sorted by
name.

### Create a store

`POST api/knowledge/stores` with
`{"name": "heating", "description": "...", "size_limit": 1000000}`.
`description` and `size_limit` are optional. The response is 201 with the
store. A name has 1 to 64 letters, digits, `_` or `-`, and starts with a
letter or a digit.

Errors: 409 `name_conflict` (names do not depend on case), 422
`invalid_name`, 422 `invalid_size_limit`.

### Read, edit and delete a store

- `GET api/knowledge/stores/{store_id}` returns the store.
- `PATCH api/knowledge/stores/{store_id}` with any of `name`,
  `description` and `size_limit`. It returns the store. A size limit below
  the current size fails with 409 `size_limit`.
- `DELETE api/knowledge/stores/{store_id}` deletes the store, its items,
  their history and their index entries. It returns
  `{"deleted": "<store ID>"}`. It fails with 409 `store_in_use` while a
  live agent attaches the store, or while the store is the private store
  of a live agent. `details.agent_ids` lists the agents. Detach the store
  first, or delete the agent with its private store.

To attach a store to an agent, edit the agent definition (`knowledge`).

### List items

`GET api/knowledge/stores/{store_id}/items` returns the items, sorted by
path:

```json
{"items": [
  {"path": "MAIN.md", "kind": "file", "version": 4, "size": 812,
   "updated_at": "<time>", "updated_by_agent_id": "<agent ID or null>"},
  {"path": "boiler.pdf", "kind": "document", "version": 1, "size": 40210,
   "updated_at": "<time>", "updated_by_agent_id": null,
   "media_type": "application/pdf", "pages": 24}
]}
```

`updated_by_agent_id` is `null` for a change by the user.

### Read an item

`GET api/knowledge/stores/{store_id}/items/{path}` returns the item fields
and its text in `content`. The optional query `section` selects a part:

- a heading, for example `section=Pressure`;
- a page of a document, for example `section=page 3`;
- a part of a large item, for example `section=part 2`.

An item with more than 20000 characters returns `part 1` when the request
names no section. A part response adds `part`, `parts`, `outline` (the
headings and pages) and `next_section`.

### Write a file

`PUT api/knowledge/stores/{store_id}/files/{path}` with
`{"content": "<Markdown>", "base_version": 3}` writes the whole text of a
file. Omit `base_version` to create a new file. It returns
`{"store_id": "...", "path": "...", "version": 4, "size": 120}`.

Errors: 409 `version_conflict` (`details.version` is the current version),
409 `version_required`, 409 `document_read_only` (the path is a document),
413 `size_limit`, 422 `invalid_path`.

### Delete an item

`DELETE api/knowledge/stores/{store_id}/items/{path}` deletes a file or a
document. The optional query `base_version` checks the version. The
response has the version of the delete. The history keeps earlier
versions, so the user can restore a deleted file. The retention limits in
"Change history and restore" apply.

### Change history and restore

- `GET api/knowledge/stores/{store_id}/history/{path}` returns
  `{"versions": [...]}`, newest first. Each version has `version`, `kind`,
  `change` (`create`, `append`, `replace`, `put`, `delete`, `upload` or
  `restore <version>`; `replace` is also a new document upload),
  `deleted`, `author` (`agent` or
  `user`), `author_agent_id`, `size` and `created_at`.
- `GET api/knowledge/stores/{store_id}/history/{path}?version=3` returns
  one version with its text in `content`. Document versions and deletes
  have `content` set to `null`.
- `POST api/knowledge/stores/{store_id}/restore` with
  `{"path": "rooms.md", "version": 3, "base_version": 5}` writes the text
  of version 3 as a new version. `base_version` is optional. It returns
  the same object as "Write a file". Only file versions with text can be
  restored: other versions fail with 409 `not_restorable`.

Retention: the app deletes old versions after each change. It keeps the
newest version of each path, and at most 50 versions of one path. The text
of the earlier versions of a store stays in total at or below the store
size limit. The app deletes the oldest versions first. A version number is
never used twice.

### Upload a document

`POST api/knowledge/stores/{store_id}/documents?name=<file name>` uploads a
document. The body is the raw file, not a form. The `Content-Type` header,
or else the file extension, gives the type:

| Type | Content type | Extensions |
|---|---|---|
| PDF with a text layer | `application/pdf` | `.pdf` |
| Markdown | `text/markdown` | `.md`, `.markdown` |
| Plain text | `text/plain` | `.txt`, `.text` |
| HTML | `text/html` | `.html`, `.htm` |

The app changes characters that a path cannot hold to `_`, for example
`Manual (2019).pdf` becomes `Manual _2019_.pdf`. The response is 201:

```json
{"store_id": "...", "path": "Manual _2019_.pdf", "version": 1,
 "size": 40210, "media_type": "application/pdf", "pages": 24,
 "sections": 22, "passages": 61}
```

`PUT api/knowledge/stores/{store_id}/documents/{path}` replaces a document
with a new upload. The response is 200 with the same object.

The upload limit is 25000000 bytes (option `knowledge.max_upload_bytes`).

Errors: 409 `document_exists` (use PUT), 409 `path_in_use` (a file uses
the path), 413 `upload_too_large`, 413 `size_limit`, 415
`unsupported_document`, 422 `extract_failed` (for example a PDF with no
text layer).

### Search

`GET api/knowledge/search?q=<text>&store_id=<ID>&limit=10` runs the hybrid
search of `knowledge.search`. Repeat `store_id` to name several stores.
With no `store_id`, it searches all stores. `limit` is 1 to 50.

```json
{"mode": "hybrid", "fallback_reason": null,
 "results": [
   {"collection": "knowledge", "store_id": "...", "source_id": "boiler.pdf",
    "title": "boiler.pdf", "page": 12, "heading": "Pressure",
    "text": "...", "score": 0.0325, "matched": ["keyword", "vector"]}
 ]}
```

`mode` is `keyword` when the search could not use vectors.
`fallback_reason` then says why: sqlite-vec did not load, no embedding
model is set, or the embedding provider failed.

### Status

`GET api/knowledge/status` returns the state of the search index:

```json
{"search_mode": "hybrid", "fallback_reason": null, "sqlite_vec": true,
 "embedding": {"provider": "openai", "model": "text-embedding-3-small",
               "dimensions": null},
 "pending_embeddings": 0, "last_error": null,
 "budget_skipped_agents": [], "entity_catalog": 214}
```

`pending_embeddings` is the number of texts that wait for a vector.
`budget_skipped_agents` lists the agents whose budget refused the
embeddings of their text. The app embeds the text of other agents, the
documents and the entity catalogue first, and tries these agents again
later. This does not change `search_mode`.
`entity_catalog` is the number of HA entities in the search index.

### Settings

- `GET api/knowledge/settings` returns `{"embedding": <model or null>}`.
- `PUT api/knowledge/settings` with
  `{"embedding": {"provider": "openai", "model": "text-embedding-3-small",
  "dimensions": 512}}` selects the embedding model of the installation.
  `provider` is `openai` or `openrouter`. `dimensions` is optional. Send
  `{"embedding": null}` to turn vector search off. A change of model
  starts a full re-index in the background. Embedding sends text to the
  provider: the UI must state this when the user selects a model (design
  section 13.3).

The default comes from the options `knowledge.embedding_provider`,
`knowledge.embedding_model` and `knowledge.embedding_dimensions`. A model
that the user selects replaces the default.

Other options of the `knowledge` section: `max_upload_bytes`,
`default_size_limit`, `max_vector_distance` (the cosine distance
above which a vector match is not a result, 0.75 by default; the right
value depends on the model), `embed_batch_size`, `embed_retry_seconds` and
`load_sqlite_vec`.

## Settings

These routes manage the settings of the installation (design section 7.2).
The settings area of the UI uses them, and also `api/knowledge/settings`
for the embedding model.

Terms:

- **Stored value**: a value that the user set. `null` means that the user
  set no value.
- **Default**: the value from the app options. The app uses it when the
  stored value is `null`.
- **Hint**: the masked form of an API key: `…` and the last four
  characters. A key with fewer than 20 characters has no hint.

The API never returns an API key, also not in an error or a change event.
The app never writes a key to the log.

### Settings object

`GET api/settings` returns the settings object:

```json
{
  "keys": {
    "openai": {"set": true, "hint": "…abcd", "source": "settings"},
    "openrouter": {"set": false, "hint": null, "source": null}
  },
  "settings": {
    "installation_daily_budget_usd": 5.0,
    "agent_daily_budget_usd": null,
    "price_overrides": null,
    "genai_prices_auto_update": null,
    "decision_models": null,
    "approvals": null
  },
  "defaults": {
    "installation_daily_budget_usd": 5.0,
    "agent_daily_budget_usd": 0.5,
    "price_overrides": {},
    "genai_prices_auto_update": false,
    "decision_models": [],
    "approvals": null
  },
  "built_in_decision_models": [
    {"provider": "openai", "model": "gpt-6-luna"}
  ]
}
```

`keys.<provider>.source` says where the key in use comes from:
`settings` (the UI), `environment` (`HAGENT_OPENAI_API_KEY` or
`HAGENT_OPENROUTER_API_KEY`), `options` (the `providers` option section),
or `null` (no key). A key in the settings wins over the other sources.

The settings:

| Setting | Content |
|---|---|
| `installation_daily_budget_usd` | The daily budget of the installation in USD, 0 to 10000. |
| `agent_daily_budget_usd` | The default daily budget of an agent in USD, 0 to 10000. It applies to an agent whose definition has `budget.daily_usd` set to `null`, and it is the budget of a new agent in `GET api/agents/defaults`. |
| `price_overrides` | Prices in USD per million tokens, keyed by `"<price provider>:<model>"`, for example `"openai:gpt-6"`. Each value has `input`, and can have `output`, `cache_read` and `cache_write`. An override wins over the other price sources. |
| `genai_prices_auto_update` | `true` lets genai-prices fetch its newest price data from GitHub once an hour. The default is `false`. |
| `decision_models` | Extra decision models, as `{"provider": ..., "model": ...}` objects. They extend `built_in_decision_models`. |
| `approvals` | The approval settings (see "Approval settings"). When it is `null`, the app uses the `approvals` option section. |

A change takes effect for the next call. It needs no restart.

### Change settings

`PATCH api/settings` with an object that names some settings changes only
those settings. A setting with the value `null` returns to its default.
The response is the settings object.

```json
{"installation_daily_budget_usd": 3, "price_overrides": null}
```

Errors: 400 `invalid_json`, 400 `invalid_body` (the body is not an
object), 422 `settings_invalid` (`details` lists the errors in the format
of `definition_invalid`; an unknown setting is an error too).

### Approval settings

```json
{"approvers": ["<HA user ID>"],
 "notify_services": {"<HA user ID>": ["notify.mobile_app_<device>"]},
 "timeout_seconds": 300}
```

The checks are the same as for the `approvals` option section (see
"Approval options"). `approvers` holds HA user IDs (32 hexadecimal
characters), each at most once. A key of `notify_services` must be an
approver. The app stores the service names without `notify.`.
`timeout_seconds` is above 0 and at most 3600. The option
`approvals.dev_trust_user_header` stays an option.

### API keys

- `PUT api/settings/keys/{provider}` with `{"key": "<API key>"}` stores
  the key of `openai` or `openrouter`. A key has 8 to 512 printable ASCII
  characters with no spaces. The response is the settings object.
- `DELETE api/settings/keys/{provider}` deletes the stored key. The app
  then uses the environment or the option key, if there is one. The
  response is the settings object.

Errors: 400 `invalid_body` (the body is not `{"key": ...}`), 404
`not_found` (an unknown provider), 422 `invalid_key`.

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

## Approvals

An **approval request** holds one tool call that the definition marks for
approval (design section 8). The app sends an HA notification with accept
and reject actions to the approvers. An approver can also answer through
this API. The first valid answer wins.

The answer route needs the `X-Remote-User-Id` header. The Supervisor sets
this header on each Ingress request to the ID of the HA user who is logged
in. It removes a copy that the client sends. Only a user in
`approvals.approvers` can answer. The HA user of the app token can never
answer.

The app trusts this header only when the TCP peer is the Supervisor
address, `172.30.32.2`. From any other peer, the app ignores the header,
and an answer gets 403 `no_user`. This applies also when
`ingress.allowed_addresses` admits more addresses. For local development,
the option `approvals.dev_trust_user_header` turns this check off. The app
then logs a warning at start.

### List approval requests

`GET api/approvals?status=<status>&limit=<n>`

Returns the newest requests first. `status` is optional. `limit` is 1 to
500. The default is 100.

```json
{"approvals": [<approval request>], "can_answer": true}
```

`can_answer` is true when the user of the request is an approver.

### Read an approval request

`GET api/approvals/{id}`

Returns one approval request, with `answers` and `can_answer`. `answers`
lists every answer that the app received for the request, oldest first:
`received_at`, `decision`, `user_id`, `via` and `outcome`. `outcome` is
one of these:

| Outcome | Meaning |
|---|---|
| `applied` | The answer decided the request. |
| `late` | The request was not pending any more. The answer had no effect. |
| `not_approver` | The HA user is not an approver. |
| `no_user` | The answer had no HA user, for example an event from an automation. |
| `app_user` | The answer came from the HA user of the app token. |

Here `can_answer` is true only while the request is pending.

### Answer an approval request

`POST api/approvals/{id}/answer`

```json
{"decision": "accept"}
```

`decision` is `accept` or `reject`. The response is the changed approval
request.

Errors:

| Status | Code | Cause |
|---|---|---|
| 400 | `invalid_body` | `decision` is not `accept` or `reject`. |
| 403 | `no_user` | The request has no `X-Remote-User-Id` header. |
| 403 | `not_approver` | The user is not an approver. |
| 404 | `approval_not_found` | No request has this ID. |
| 409 | `approval_closed` | The request is not pending. |

### Approval request object

```json
{
  "id": "<32 hex characters>",
  "status": "pending",
  "agent_id": "<agent ID>",
  "agent_version": 3,
  "invocation_id": "<invocation ID>",
  "tool": "ha.call_service",
  "summary": "Agent 'Heating' asks to run ha.call_service.\nServices: climate.set_temperature\nEntities: climate.<room>",
  "request": {"domain": "climate", "service": "set_temperature"},
  "approvers": ["<HA user ID>"],
  "targets": ["mobile_app_<device>"],
  "problems": [],
  "created_at": "2026-11-15T10:00:00+00:00",
  "expires_at": "2026-11-15T10:05:00+00:00",
  "finished_at": null,
  "answered_by": null,
  "answered_via": null,
  "reason": null
}
```

| Status | Meaning |
|---|---|
| `pending` | The agent turn waits for an answer. |
| `accepted` | An approver accepted the call. The executor runs it, if the agent is still live and on, with the same definition version. Otherwise the tool result has `approval_stale`, and `reason` says why. |
| `rejected` | An approver rejected the call. The tool result has `approval_rejected`. |
| `timeout` | No valid answer arrived before `expires_at`. The tool result has `approval_timeout`. |
| `failed` | No notification reached an approver. `problems` says why. The tool result has `approval_unavailable`. |
| `cancelled` | The agent turn stopped while it waited, or the agent changed (deleted, turned off or a new version). For an agent change, the tool result has `approval_stale`. |
| `expired` | The app stopped or restarted while the request was pending. |

- `summary` is the notification text. It names only the checked services
  and entities, never free-form service data. For `ha.admin_change`, it
  also names the command type and the ID fields of the command
  (`entity_id`, `device_id`, `area_id`, `floor_id`, `label_id`,
  `dashboard_id`, `url_path` and `<helper domain>_id`) when the value is a
  plain ID (`[a-z0-9_.-]`, at most 100 characters).
- `targets` lists the notify services that received the notification.
- `problems` lists why an approver or a device received no notification.
- `answered_via` is `notification` or `api`.

### Approval options

The `approvals` section of the app options has these keys. Approval
settings that the user sets in the UI replace `approvers`,
`timeout_seconds` and `notify_services` (see "Approval settings").

| Key | Default | Content |
|---|---|---|
| `approvers` | `[]` | The HA user IDs that can answer. With an empty list, each call that needs approval fails with `approval_unavailable`. |
| `timeout_seconds` | `300` | How long an agent turn waits. Above 0 and at most 3600. |
| `dev_trust_user_header` | `false` | Development only. Trust `X-Remote-User-Id` from every allowed address, not only from the Supervisor. |
| `notify_services` | `{}` | An optional list of `notify.mobile_app_<device>` services for each approver. It replaces the person link for that approver. |

Without `notify_services`, the app finds the phones of an approver through
the HA `person` entity whose user is the approver, then its device trackers
of the mobile app. The app sends to a service only if the service exists
and exactly one mobile app device has its name. Otherwise it records the
problem and does not send.

Limit: a user can link any device tracker to a person. A notification can
therefore reach the phone of another person. That phone cannot approve,
because the app checks the HA user that sends each answer.

## Integration link

The HA integration connects to the app over a WebSocket, the **integration
link**. `docs/link.md` describes the link protocol. The management UI does
not use the link.

`GET api/link/ws` opens the link. It is the only path that the Ingress
guard does not check (the exact path, not its subpaths), because HA Core connects from its own address. The
request must carry `Authorization: Bearer <secret>`. Without the right
secret, the app answers 401 `unauthorized` and does not open the WebSocket.

### Set-up information

`GET api/integration` returns what the integration set-up needs. The
Ingress guard checks this route like every other route, so only HA
administrators can read the secret.

```json
{"host": "<app host name>", "port": 8099, "path": "/api/link/ws",
 "secret": "<shared secret>", "protocol": 1, "connections": 1}
```

- `host`: the host name of the app on the internal network.
- `secret`: the shared secret. The app creates it on the first start and
  keeps it in the file `link_secret` in the data directory (mode 0600).
- `connections`: the number of open link connections.

### Link options

The `link` section of the app options has these keys:

| Key | Default | Content |
|---|---|---|
| `discovery` | `true` | Send the host, the port and the secret to the Supervisor. HA then offers to set up the integration. It applies only inside an app container. |
| `supervisor_url` | `"http://supervisor"` | The Supervisor API. |
| `host` | `""` | The host name for the integration. An empty value selects the host name of the container. |
| `refresh_seconds` | `300` | The interval of the full agent refresh on the link (cost today and budget state). |

## Rules

A **rule** is a fast path that an agent writes with the `self` tools
(design section 10). The agent owns its rules. The user can read them, roll
them back, turn them off, delete them and run backtests in the management
UI. Rule IDs, like other IDs,
are 32 hexadecimal characters.

Terms:

- **Rule version**: one spec of a rule that never changes. A change adds a
  version. The current version answers invocations (design section 10.6).
- **Backtest**: a run of one rule version against the agent's recorded
  history. It writes nothing (design section 10.7).
- **Case**: one past invocation in a backtest.
- **Trial**: an agent turn that runs next to a rule that answered. It
  writes nothing. Only the rule result takes effect (design section 10.1).
- **Agreement template**: the `agreement` template of the rule. It receives
  `rule_result` and `agent_result`, and renders true when they agree. A rule
  without it agrees when the two results have the same JSON form.

Every route under `api/agents/{id}/rules` reads or changes the rules of one
live agent only. A rule of another agent gives 404 `rule_not_found`.

Errors of the rule routes:

| Status | Code | Cause |
|---|---|---|
| 400 | `invalid_body` | A field has the wrong type or is out of range. |
| 404 | `agent_not_found` | The agent does not exist, or the user deleted it. |
| 404 | `rule_not_found` | The agent has no rule with this ID, or it deleted the rule. |
| 404 | `version_not_found` | The rule has no version with this number. |
| 404 | `backtest_not_found` | The rule has no backtest with this ID. |
| 409 | `backtest_running` | A backtest of this rule runs already. |
| 409 | `rule_is_draft` | The user tried to activate a draft rule. |
| 422 | `invalid_rule` | The stored spec of the version is not valid any more. |
| 503 | `not_ready` | The rule engine is not running. |

### List rules

`GET api/agents/{id}/rules`

Returns the rules of the agent in the order that the router tests them:

```json
{"rules": [{"id": "<32 hex>", "position": 0, "state": "active",
  "current_version": 2, "latest_version": 2,
  "updated_at": "2026-11-15T10:00:00+00:00", "spec": {"name": "...", "...": "..."}}]}
```

`state` is `draft`, `active` or `off`. Only active rules answer
invocations. `spec` is the current version. The rule spec is described in
the app README, section "Rules".

### Read a rule

`GET api/agents/{id}/rules/{rule_id}`

Returns the rule with the spec of the current version, and the statistics
of each version (design section 10.4):

```json
{"id": "<32 hex>", "rule_id": "<32 hex>", "state": "draft",
 "current_version": 2, "latest_version": 2, "deleted": false,
 "spec": {"...": "..."},
 "versions": [{"version": 1, "created_at": "...", "matched": 10,
   "answered": 9, "declined": 1, "cache_hits": 0, "errors": 0,
   "cost_usd": 0.0, "last_used_at": "...", "last_error": null,
   "last_error_at": null,
   "backtest": {"runs": 1, "cost_usd": 0.0, "agreement_rate": 0.9,
                "last": <backtest without cases>},
   "trial": {"runs": 5, "agreed": 4, "disagreed": 1, "errors": 0,
             "budget": 0, "agreement_rate": 0.8, "cost_usd": 0.004}}]}
```

- The live counters (`matched` to `cost_usd`) count real invocations only.
  Backtests and trials never change them.
- `backtest.agreement_rate` is the rate of the newest backtest of the
  version. `backtest.cost_usd` is the sum of all its backtests.
- `trial.agreement_rate` is agreed trials divided by agreed and disagreed
  trials. `trial.budget` counts trials that the budget stopped.

### Read a version

`GET api/agents/{id}/rules/{rule_id}/versions/{version}`

```json
{"rule_id": "<32 hex>", "version": 1, "spec": {"...": "..."}}
```

### Roll a rule back

`POST api/agents/{id}/rules/{rule_id}/rollback`

```json
{"version": 1}
```

The version becomes the current version. Its statistics continue. The state
of the rule does not change. The response is
`{"id": "<rule ID>", "version": 1, "state": "active"}`.

### Turn a rule off or on

`POST api/agents/{id}/rules/{rule_id}/state`

```json
{"state": "off"}
```

`state` is `off` or `active`. `off` stops the rule: the router does not
test it, so the agent turn answers. `active` turns an `off` rule on again.
The user cannot activate a `draft` rule. The agent activates a draft after
its backtest (design section 10.7). The current version does not change.
The response is `{"id": "<rule ID>", "version": 2, "state": "off"}`.

Errors: 400 `invalid_body` for another `state`, 404 `rule_not_found` for
a rule of another agent or a deleted rule, and 409 `rule_is_draft` for
`active` on a draft rule.

### Delete a rule

`DELETE api/agents/{id}/rules/{rule_id}`

The rule stops at once and leaves the rule list. Its versions and their
statistics stay for history. The response is `{"deleted": "<rule ID>"}`.
A deleted rule gives 404 `rule_not_found` for a later change.

### Run a backtest

`POST api/agents/{id}/rules/{rule_id}/backtests`

```json
{"version": 2, "days": 7, "limit": 20, "live": true}
```

Every field is optional.

| Field | Default | Rules |
|---|---|---|
| `version` | the current version | 1 or more. A draft version can run a backtest. |
| `days` | 7 (option `backtest.default_days`) | 1 to `backtest.max_days` (30). The window ends now. |
| `limit` | 20 for a decision action (option `backtest.decision_cases`), else 1000 | 1 to `backtest.max_cases` (1000). The most matched invocations. |
| `live` | `true` | `false` replays recorded decision answers only. |

The app runs the backtest and answers when it ends. The steps:

1. Select the past invocations of the agent in the window that have an
   agent result: an agent turn that answered (`path` `agent_turn`, `status`
   `ok`), or a finished trial. Dry runs, review, Assist and test
   invocations do not count. The newest come first.
2. For each one, render the match template. If it matches, run the rule
   with the recorded values (see "Replay rules"). The backtest stops after
   `limit` matched invocations.
3. Compare the rule result with the agent result with the agreement
   template.
4. Store the counters, the agreement rate, the cost and each case.

Replay rules:

- `now()` returns the time of the past invocation, in HA's time zone.
- The input entities have their recorded states. The other entities in the
  read scope have their current states.
- Context items: a value that a rule recorded for the invocation comes
  first. A `history` item reads statistics for the window that ends at the
  time of the invocation. An `invocation_history` item reads the
  invocations before it. An `action_response` item uses the response of the
  same action in the agent turn of the invocation; without it, the case is
  `skipped`. A `store_file` item uses the same read in the turn, else the
  current file. Each item gets the scope check of the live rule.
- Decision actions: the backtest replays the recorded answer of the same
  request (the same model, values and questions) with no cost. Without one,
  it sends the request if `live` is true, else it skips the case. Each live
  request counts towards the budget of the agent. When the budget is spent,
  the backtest stops with the status `budget`.
- Writes: the backtest checks each rendered write against the scope and
  the approval rules, and records it in the case. A write that fails the
  check makes the case `error`. The backtest never sends a write to HA, and
  writes no audit row.

Response (status 200): a backtest object with its cases.

```json
{"id": "<32 hex>", "rule_id": "<32 hex>", "version": 2, "agent_version": 3,
 "status": "ok", "started_at": "...", "finished_at": "...",
 "since": "...", "until": "...", "max_cases": 20,
 "candidates": 12, "matched": 10, "no_match": 2, "compared": 9,
 "agreed": 8, "disagreed": 1, "declined": 1, "errors": 0, "skipped": 0,
 "live_decisions": 4, "replayed_decisions": 6,
 "agreement_rate": 0.889, "cost_usd": 0.0004, "error": null,
 "cases": [{"seq": 1, "invocation_id": "<32 hex>",
   "invocation_at": "...", "outcome": "agreed", "decision": "replayed",
   "rule_result": 42.0, "agent_result": 43,
   "writes": [{"service": "input_number.set_value",
               "entity_id": "input_number.<flow>", "data": {"value": 42.0}}],
   "detail": null}]}
```

- `status` is `ok`, `budget` (the budget stopped it), `failed` or
  `cancelled` (the app stopped).
- `outcome` is `agreed`, `disagreed`, `no_match`, `declined`, `error` or
  `skipped`. `detail` says why for `declined`, `error` and `skipped`.
- `agreement_rate` is `agreed / compared`, or `null` when no case was
  compared. Skipped, declined and failed cases do not count.
- `decision` is `replayed` or `live` for a decision action, else `null`.

### List backtests

`GET api/agents/{id}/rules/{rule_id}/backtests`

Returns `{"backtests": [...]}`: the newest 50 backtests of the rule, without
`cases`.

`GET api/agents/{id}/rules/{rule_id}/backtests/{backtest_id}` returns one
backtest with its cases. The app deletes the cases after the retention
period (option `backtest.retention_days`, 30 days). The summary stays.

### List trials

`GET api/agents/{id}/rules/{rule_id}/trials`

Returns `{"trials": [...]}`: the newest 50 trials of the rule.

```json
{"id": "<32 hex>", "rule_id": "<32 hex>", "version": 2,
 "invocation_id": "<32 hex>", "turn_id": "<32 hex>",
 "created_at": "...", "finished_at": "...", "status": "agreed",
 "rule_result": 42.0, "agent_result": 43, "detail": null,
 "cost_usd": 0.0012}
```

`status` is `running`, `agreed`, `disagreed`, `error`, `budget` (the budget
was spent before or during the turn) or `cancelled`.

### Check a template

`POST api/rules/check-template`

```json
{"template": "{{ states('sensor.<room>_temperature') | float > 20 }}"}
```

The app checks the template against the supported subset
(`docs/rule-templates.md`): syntax, functions, filters, tests and blocked
statements. It does not render it. The response is
`{"ok": true, "error": null}`, or `{"ok": false, "error": "<message>"}`
with status 200.

## Inspect

The inspect routes are read-only (design section 7.2, "Inspect"). They
read the records that the runtime writes: invocations, agent turns, the
tool audit log, provider calls, schedules and subscriptions. The UI reads
rules, backtests and trials through the rule routes, and approval requests
through the approval routes.

Terms:

- **Invocation**: one request to an agent, from any source (design section
  9). The router writes one record for each invocation.
- **Trace**: the full record of one invocation: the request, the agent
  turns with their messages, each tool call with its scope decision, each
  compaction, the result and the cost.
- **Scope decision**: `allowed` or `denied`. The executor writes an audit
  row for each denied call and each change to HA. A tool call without an
  audit row passed the scope check, so its decision is `allowed`.
- **Effective scope**: the HA scope of an agent, resolved to entity IDs
  with the current HA registries and states.

Rules for all inspect routes:

- No route returns an API key, the link secret or an approval action ID.
  The app replaces each approval action ID and approval tag with
  `[redacted]`.
- `limit` is 1 to 500. The default is 100.
- `agent_id` is optional where this section does not put it in the path.
  It must be 32 hexadecimal characters.
- A query parameter that is not valid gives 400 `invalid_query`.
- Before the app is ready, each route gives 503 `not_ready`.
- Money fields end in `_usd` and hold US dollars as JSON numbers.

### List invocations

`GET api/inspect/invocations?agent_id=&source=&path=&status=&q=&before=&dry_run=&limit=`

Returns the newest invocations first:

```json
{"invocations": [{"id": "<32 hex>", "agent_id": "<32 hex>",
  "source": "invoke", "source_key": "invoke", "conversation_id": null,
  "dry_run": false, "created_at": "...", "started_at": "...",
  "finished_at": "...", "status": "ok", "path": "agent_turn",
  "fallback_reason": null, "error_code": null, "agent_version": 3,
  "rule_id": null, "rule_version": null, "turn_id": "<turn ID>",
  "cost_usd": 0.0021}]}
```

| Parameter | Content |
|---|---|
| `source` | The source of the invocation, for example `invoke`, `schedule` or `assist`. |
| `path` | `rule`, `agent_turn` or `fallback`. |
| `status` | `queued`, `running`, `ok`, `fallback`, `error` or `cancelled`. |
| `q` | The history search. A text match that ignores case, on the request, the result and the error of the invocation, and on the input and the result of its agent turns. At most 200 characters. |
| `before` | An ISO time. Only invocations created before it. Use the `created_at` of the last item to load the next page. |
| `dry_run` | `true` adds test invocations. The default is `false`. |

`cost_usd` is the cost that the router recorded when the invocation ended.
The trace sums the provider calls, which can include later background
cost.

### Read an invocation trace

`GET api/inspect/invocations/{invocation_id}`

```json
{"invocation": {"...": "the list fields", "request": {}, "result": 21.5,
                "error": null},
 "turns": [{"id": "<turn ID>", "status": "ok", "stop_reason": null,
   "input": {}, "prompt": "...", "result": 21.5, "messages": [],
   "usage": {"requests": 2, "tool_calls": 1, "input_tokens": 900,
             "cache_read_tokens": 600, "cache_write_tokens": 0,
             "output_tokens": 40},
   "tool_calls": [{"seq": 1, "tool": "ha.call_service", "arguments": {},
     "ok": true, "error_code": null, "result": {}, "result_chars": 2,
     "cut": false, "scope_decision": "allowed", "audit": <audit entry>}],
   "compactions": [{"id": 1, "kind": "tool_result_cut", "detail": {},
                    "summary": null}]}],
 "audit": [<audit entry>],
 "provider_calls": [{"id": 1, "call_type": "chat", "provider": "openai",
   "model": "...", "status": "ok", "input_tokens": 900,
   "cache_read_tokens": 600, "cache_write_tokens": 0, "output_tokens": 40,
   "cost_usd": 0.0021, "cost_source": "price_table"}],
 "trials": [{"id": "<32 hex>", "rule_id": "<32 hex>", "version": 2,
   "status": "agreed", "rule_result": 21.5, "agent_result": 21.5,
   "cost_usd": 0.0012}],
 "approvals": [{"id": "<32 hex>", "tool": "ha.call_service",
                "status": "accepted"}],
 "cost": {"total_usd": 0.0021, "input_tokens": 900,
          "cache_read_tokens": 600, "cache_write_tokens": 0,
          "output_tokens": 40}}
```

- `turns` lists every agent turn of the invocation, also trial turns. A
  rule answer has no turn.
- `audit` lists every audit row of the invocation, also the writes of a
  rule action.
- `invocation.rule_id` and `invocation.rule_version` name the rule
  version that answered. Read its spec with "Read a version" in "Rules".
- `tool_calls[].audit` is the audit row of the call, or `null`.

Error: 404 `invocation_not_found`.

### Audit log

`GET api/inspect/audit?agent_id=&decision=&tool=&before=&limit=`

Returns `{"entries": [<audit entry>]}`, newest first. This is the "Changes
to HA" log. `decision=allowed` lists the changes only, `decision=denied`
the denied calls only. `before` is an audit row ID: only older rows.

```json
{"id": 12, "created_at": "...", "finished_at": "...",
 "agent_id": "<32 hex>", "agent_version": 3, "invocation_id": "<32 hex>",
 "tool": "ha.admin_change", "level": "admin", "request": {},
 "decision": "allowed", "reason_code": null, "reason": null,
 "status": "ok", "error": null, "context_id": "...",
 "previous": {"...": "the item before the change"}, "previous_note": null}
```

- `status` is `pending`, `ok`, `error`, `denied` or `dry_run`.
- `previous` is the previous version of the item of an `admin` change.
  `previous_note` says why `previous` is empty, if it is.

### Permissions of an agent

`GET api/inspect/agents/{id}/permissions?limit=`

Returns the effective scope of the current definition and the newest
denied calls of the agent (`limit`, default 100):

```json
{"agent_id": "<32 hex>", "version": 3, "registry_stale": false,
 "ha": {"level": "control", "services": [], "outputs": [],
        "read": {"all": false, "entities": ["light.<room>"],
                 "domains": ["weather"]},
        "control": {"all": false, "entities": ["light.<room>"],
                    "domains": []}},
 "tools": [{"name": "ha.call_service", "group": "ha"}],
 "knowledge": [{"store_id": "<32 hex>", "access": "read"}],
 "approval": {"tools": [], "services": [], "entities": []},
 "denied": [<audit entry>]}
```

- `entities` lists each entity that the scope permits now, also each
  entity of a permitted domain.
- `all` is true for the `admin` level. Then `entities` is empty.
- The read scope includes the inputs and the outputs.
- `registry_stale` is true when the app uses the last known HA registries.
  When the scope needs registries that the app never loaded, `ha` has
  `unavailable` with a reason, and no `read` or `control`.

Error: 404 `agent_not_found`.

### Schedules and subscriptions

`GET api/inspect/schedules?agent_id=`

```json
{"items": [{"id": "<item ID>", "agent_id": "<32 hex>",
   "kind": "schedule", "spec": {}, "enabled": true, "run_at": null,
   "created_at": "...", "updated_at": "...",
   "last_run": {"due_at": "...", "at": "...", "status": "submitted",
                "missed_count": 1, "invocation_id": "<32 hex>",
                "result_status": "ok", "result_code": null},
   "last_event": null}],
 "reviews": [{"agent_id": "<32 hex>", "at": "...", "due_at": "...",
              "status": "submitted", "invocation_id": "<32 hex>"}],
 "runs": [], "events": []}
```

- `kind` is `schedule` or `subscription`. `last_run` is for a schedule,
  `last_event` for a subscription.
- `reviews` gives the last run of the review schedule of each agent.
- `runs` and `events` list the newest 50 schedule runs and subscription
  events (design section 12).

### Cost

`GET api/inspect/cost?days=&agent_id=`

Returns the provider cost of the last `days` days (1 to 366, default 30),
by local day of HA, with today included:

```json
{"since": "2026-10-17",
 "total": {"calls": 40, "cost_usd": 0.42, "embedding_cost_usd": 0.01,
           "input_tokens": 50000, "cache_read_tokens": 30000,
           "cache_write_tokens": 0, "output_tokens": 2000},
 "by_agent": [{"agent_id": "<32 hex>", "...": "the total fields"}],
 "by_day": [{"day": "2026-11-15", "...": "the total fields"}],
 "by_path": [{"path": "agent_turn", "...": "the total fields"}],
 "by_type": [{"call_type": "chat", "...": "the total fields"}]}
```

- `by_agent` has `agent_id` `null` for cost that belongs to no agent, for
  example a document embedding.
- `path` is the path of the linked invocation: `rule`, `agent_turn` or
  `fallback`. A call without an invocation has `embedding` or `other`. An
  invocation that did not finish has `none`.
- `call_type` is `chat`, `decision` or `embedding`.

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
| `approvals` | `created` | `status`: `pending`. `id` is the approval request ID. |
| `approvals` | `updated` | `status`: the final status. |
| `knowledge` | `created`, `updated`, `deleted` | none. `id` is the store ID. |
| `knowledge` | `file_changed`, `file_deleted`, `item_deleted`, `document_changed` | `path`. `id` is the store ID. |
| `knowledge` | `settings_changed` | none. `id` is `settings`. |
| `settings` | `updated` | none. `id` is `settings`. The app sends it after each change of the settings or of an API key. |
| `rules` | `updated` | `agent_id`, `version`. The app sends it after a rollback or a state change through the API. `id` is the rule ID. |
| `rules` | `deleted` | `agent_id`. The app sends it after a delete through the API. `id` is the rule ID. |
| `rules` | `backtest_finished` | `agent_id`, `backtest_id`. `id` is the rule ID. |
| `invocations` | `activity` | `running`: the number of invocations of the agent that run now. The app sends it when an invocation starts or ends. `id` is the agent ID. |
| `invocations` | `finished` | `agent_id`, `status`, `path`. The app sends it when an invocation ends. `id` is the invocation ID. |
| `tools` | `scope_grown` | `version`, `added_read`, `added_control`: lists of entity IDs. The app sends it when an HA registry change makes the scope of an agent grow. `id` is the agent ID. |

Rules for the UI:

- An event says only what changed. Load the record again through the API.
- After a reconnect, load all data again. The app does not repeat the events
  that the UI missed.
- If a client reads too slowly, the app closes its connection. The client
  then reconnects.

Later tasks add topics, for example for knowledge stores and rules. They
add their topics to this table.
