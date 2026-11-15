# Rule templates

This document defines the template subset that rules can use (design
section 10.2). The app renders rule templates itself. It never sends them to
HA Core. Rules therefore keep working while HA Core restarts.

## Terms

- **Rule template**: one of the templates of a rule: `match`, the
  `template` action, `result`, the data of a write, and `agreement`.
- **Sandbox**: a Jinja environment that blocks access to Python internals.
  The app uses `ImmutableSandboxedEnvironment` from Jinja. It also blocks the
  methods that change lists and dicts.
- **Scoped state view**: the copy of the HA states that one agent can read.
- **Subset**: the HA template functions, filters and tests that the sandbox
  supplies. A rule that uses a name outside the subset fails when the agent
  writes it (`invalid_template`).

## Scope

A template reads states from the scoped state view only:

| `ha` level | Visible entities |
|---|---|
| `none` | The input entities. |
| `read`, `control` | The read scope: the scope, the inputs and the outputs. |
| `admin` | All entities. |

For an entity outside the view, the template sees the same as for an entity
that does not exist. `states()` returns `unknown`, `states.<domain>.<id>`
is `none`, and `is_state()` is false. Iteration over `states` or
`states.<domain>` returns visible entities only.

The router reads the states once for each invocation. All templates of the
invocation see the same states.

While HA restarts, the view uses the last known state cache and the last
known registry. If the app has no registry data yet, a scope that uses
devices, areas, floors or labels shrinks to its listed entities and domains.

A state object has `entity_id`, `domain`, `object_id`, `name`, `state`,
`attributes`, `last_changed` and `last_updated`. It has no `context`,
because the HA context holds user IDs.

## Variables

| Variable | Content | Templates |
|---|---|---|
| `source` | The invocation source, for example `invoke`. | All |
| `inputs` | The input values, keyed by entity ID. | All |
| `data` | The caller data. `data.text` holds the text, if any. | All |
| `context` | The context items, keyed by item name (design 10.5). | All after `match` |
| `output` | The action output. | `result`, writes |
| `answers` | The decision answers, keyed by question name. | `result`, writes |
| `result` | The final result. | Writes |

Each decision answer has `refused`. A yes/no answer has `probability`. A
choice answer has `choice`, `probabilities` and `confidence`. A score answer
has `score`, `probabilities` and `confidence`.

## Supported subset

State functions (also as filters, and the first three as tests):

- `states(entity_id, rounded=False, with_unit=False)`, `states.<domain>`,
  `states.<domain>.<object_id>`
- `is_state(entity_id, value)`, where `value` can be a list
- `state_attr(entity_id, name)`, `is_state_attr(entity_id, name, value)`
- `has_value(entity_id)`

Time functions:

- `now()` (in the HA time zone), `utcnow()`, `today_at("HH:MM")`
- `as_datetime`, `as_timestamp`, `as_local`, `timedelta`
- Filters: `timestamp_local`, `timestamp_utc`, `timestamp_custom`

Number and logic functions (functions and filters):

- `float`, `int`, `bool`: without a default, invalid input is an error, as
  in HA.
- `round(precision, method)`: `common`, `ceil`, `floor` or `half`.
  Precision 0 gives an integer, as in HA.
- `multiply`, `add`, `average`, `median`, `min`, `max`, `iif`, `is_number`

Text and data:

- `to_json`, `from_json`
- `regex_match`, `regex_search`, `regex_replace`, `regex_findall`,
  `regex_findall_index`, and the tests `match`, `search` and `contains`
- `format` and the `%` operator, with widths of 1000 or less
- Jinja filters: `abs`, `attr`, `batch`, `capitalize`, `center`, `count`,
  `default` (`d`), `dictsort`, `first`, `groupby`, `indent`, `items`,
  `join`, `last`, `length`, `list`, `lower`, `map`, `max`, `min`, `reject`,
  `rejectattr`, `replace`, `reverse`, `select`, `selectattr`, `slice`,
  `sort`, `string`, `sum`, `title`, `tojson`, `trim`, `truncate`, `unique`,
  `upper`, `wordcount`
- Jinja globals: `range`, `dict`, `namespace`
- The common Jinja tests, for example `defined`, `none`, `number` and `in`

String methods: only `lower`, `upper`, `title`, `capitalize`, `casefold`,
`strip`, `lstrip`, `rstrip`, `split`, `rsplit`, `splitlines`, `startswith`,
`endswith`, `find`, `rfind`, `count`, `removeprefix`, `removesuffix` and the
`is...` checks.

Not supported, among others: the area, device, floor, label and
integration functions, `expand`, `distance`, `closest`, `relative_time`,
`time_since`, `states.<domain>.<id>.context`, the `import`, `include`,
`extends`, `macro` and `call` statements, and `str.format`.

## Result values

The app turns rendered text into a value, as HA does. `5` becomes 5,
`1.50` becomes 1.5, `True` becomes true, and `[1, 'a']` becomes a list.
`01`, `1e3` and other text stay text. The app removes spaces at the start
and the end. A result template that renders `decline` passes the
invocation to an agent turn.

## Limits

A rule template is text from a model, so the sandbox limits its cost:

- A template has at most 10,000 characters.
- The rendered output has at most 256 KiB.
- Each `{{ }}` value, and each argument of every filter, test and function,
  passes a size check first. The check counts every reference, so nested
  lists that share references cannot grow without a limit.
- `*`, `**`, `+`, `%`, `~`, `join`, `replace`, `regex_replace`, `indent`,
  `center` and `format` check the size of their result before or while they
  build it.
- `batch` takes a size of 10,000 or less. `sum` adds numbers only.
- A render stops after 1 second (option `rules.render_seconds`). A trace
  function stops a loop in the template.
- The runtime waits at most 1 second more for a render. It then records
  `rule_error`, and the invocation goes on to an agent turn.

Residual risk: one slow call in C, for example a regular expression that
backtracks, cannot be stopped. The wait ends, but the thread runs on until
the call ends. Renders use a pool of 4 threads of their own. If all 4 are
stuck, later renders wait, fail at the time limit, and the rules record
`rule_error`. Other app work does not use this pool.

## Evidence against HA

`app/tests/fixtures/ha_templates/cases.json` holds HA's own output for 71
synthetic templates, recorded from HA 2026.11.1 with the read-only
`render_template` command. `app/tests/test_rule_templates_ha.py` renders
the same templates in the sandbox and compares the values and their types.
The state functions in these cases name only entities that do not exist.
The fixtures therefore compare state functions for missing entities only.
The other state behaviour has tests against a scoped view with sample
states (`app/tests/test_rule_templates.py`).

To record the fixtures again, run `record.py` in the same folder with
`HASS_URL` and `HASS_TOKEN` set.
