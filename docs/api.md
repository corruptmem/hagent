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
response has the version of the delete. The history keeps the earlier
versions, so the user can restore a deleted file.

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
 "pending_embeddings": 0, "last_error": null, "entity_catalog": 214}
```

`pending_embeddings` is the number of texts that wait for a vector.
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

The `approvals` section of the app options has these keys. The settings
store (task-20) replaces them later.

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
guard does not check, because HA Core connects from its own address. The
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
| `tools` | `scope_grown` | `version`, `added_read`, `added_control`: lists of entity IDs. The app sends it when an HA registry change makes the scope of an agent grow. `id` is the agent ID. |

Rules for the UI:

- An event says only what changed. Load the record again through the API.
- After a reconnect, load all data again. The app does not repeat the events
  that the UI missed.
- If a client reads too slowly, the app closes its connection. The client
  then reconnects.

Later tasks add topics, for example for knowledge stores and rules. They
add their topics to this table.
