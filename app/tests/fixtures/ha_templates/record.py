"""Record HA's own output for the rule template fixtures (task-13).

This is a one-off script. Tests never run it, and tests never need HA. It
sends each template in ``CASES`` to HA with the read-only
``render_template`` WebSocket command, and writes the results to
``cases.json`` next to this file. ``test_rule_templates_ha.py`` then renders
the same templates in the app sandbox and compares the results.

The templates are synthetic. The state functions name only entity IDs that
do not exist (``*.hagent_fixture_*``), so the fixtures hold no data about a
real home. No template depends on the current time.

Run it with the HA URL and a long-lived token in the environment:

    HASS_URL=... HASS_TOKEN=... uv run python \\
        app/tests/fixtures/ha_templates/record.py
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

MISSING = "sensor.hagent_fixture_missing"

# (name, template, variables)
CASES: list[tuple[str, str, dict[str, Any]]] = [
    # State functions on an entity that does not exist. The sandbox gives
    # the same answers for an entity outside the scope.
    ("states_missing", f"{{{{ states('{MISSING}') }}}}", {}),
    ("states_missing_attr", f"{{{{ states.{MISSING} }}}}", {}),
    ("states_missing_is_none", f"{{{{ states.{MISSING} is none }}}}", {}),
    ("is_state_missing", f"{{{{ is_state('{MISSING}', 'unknown') }}}}", {}),
    ("state_attr_missing", f"{{{{ state_attr('{MISSING}', 'unit') }}}}", {}),
    ("is_state_attr_missing", f"{{{{ is_state_attr('{MISSING}', 'a', 1) }}}}", {}),
    ("has_value_missing", f"{{{{ has_value('{MISSING}') }}}}", {}),
    ("states_missing_float", f"{{{{ states('{MISSING}') | float(-1) }}}}", {}),
    # Number filters and functions.
    ("float_default", "{{ 'abc' | float(0) }}", {}),
    ("float_variable", "{{ temp | float + 1 }}", {"temp": "20.5"}),
    ("float_error", "{{ 'abc' | float }}", {}),
    ("int_filter", "{{ '4' | int + 1 }}", {}),
    ("int_float_text", "{{ '4.7' | int }}", {}),
    ("int_default", "{{ 'x' | int(7) }}", {}),
    ("int_function", "{{ int('12') * 2 }}", {}),
    ("float_function", "{{ float('1.5', 0) * 2 }}", {}),
    ("bool_on", "{{ 'on' | bool }}", {}),
    ("bool_default", "{{ 'maybe' | bool('n/a') }}", {}),
    ("round_one", "{{ 2.567 | round(1) }}", {}),
    ("round_ceil", "{{ 2.4 | round(0, 'ceil') }}", {}),
    ("round_floor", "{{ 2.6 | round(0, 'floor') }}", {}),
    ("round_half", "{{ 2.3 | round(0, 'half') }}", {}),
    ("round_default", "{{ 'x' | round(1, default=0) }}", {}),
    ("multiply", "{{ 3 | multiply(2) }}", {}),
    ("add", "{{ '3' | add(2) }}", {}),
    ("average", "{{ average(1, 2, 3) }}", {}),
    ("average_list", "{{ [1, 2] | average }}", {}),
    ("median", "{{ [1, 5, 3] | median }}", {}),
    ("max_args", "{{ max(1, 7, 3) }}", {}),
    ("min_list", "{{ min([4, 2]) }}", {}),
    ("is_number_test", "{{ '12' is is_number }}", {}),
    ("is_number_filter", "{{ 'x' | is_number }}", {}),
    ("iif_function", "{{ iif(x > 1, 'big', 'small') }}", {"x": 3}),
    ("iif_none", "{{ none | iif('a', 'b', 'c') }}", {}),
    ("timedelta", "{{ timedelta(hours=2).total_seconds() }}", {}),
    # Text, JSON and regular expressions.
    ("to_json", "{{ {'a': 1, 'b': [1, 2]} | to_json }}", {}),
    ("to_json_in_text", "x{{ {'a': 1} | to_json }}", {}),
    ("from_json", "{{ ('{\"a\": [1, 2]}' | from_json).a[1] }}", {}),
    ("regex_match", "{{ 'abc' | regex_match('b') }}", {}),
    ("regex_search", "{{ 'abc' | regex_search('b') }}", {}),
    ("regex_replace", "{{ 'abc' | regex_replace('b', 'x') }}", {}),
    ("regex_findall", "{{ 'a1b22' | regex_findall('[0-9]+') }}", {}),
    ("regex_findall_index", "{{ 'a1b22' | regex_findall_index('[0-9]+', 1) }}", {}),
    ("match_test", "{{ 'abc' is match('a') }}", {}),
    ("join_text", "{{ ['a', 'b'] | join(', ') }}", {}),
    ("join_numbers", "{{ [1, 2] | join(',') }}", {}),
    ("format_filter", "{{ '%.1f' | format(2.25) }}", {}),
    ("percent_operator", "{{ '%s-%s' % ('a', 'b') }}", {}),
    ("contains_test", "{{ [1, 2] is contains 2 }}", {}),
    # Time functions with fixed values.
    ("timestamp_utc", "{{ 0 | timestamp_utc }}", {}),
    ("timestamp_custom", "{{ 0 | timestamp_custom('%Y-%m-%d', false) }}", {}),
    ("as_timestamp", "{{ as_timestamp('1970-01-01T00:00:10+00:00') }}", {}),
    ("as_datetime_year", "{{ as_datetime('2026-01-02T03:04:05+00:00').year }}", {}),
    # Result parsing: HA turns the rendered text into a native value.
    ("parse_int", "{{ 5 }}", {}),
    ("parse_leading_zero", "{{ '01' }}", {}),
    ("parse_float", "{{ 1.50 }}", {}),
    ("parse_bool", "{{ true }}", {}),
    ("parse_list", "{{ [1, 'a'] }}", {}),
    ("parse_dict", "{{ {'a': 1} }}", {}),
    ("parse_none", "{{ none }}", {}),
    ("parse_text", "hello", {}),
    ("parse_padded", "  padded  ", {}),
    ("parse_exponent", "1e3", {}),
    ("parse_quoted", "'quoted'", {}),
    ("parse_decline", "{{ 'decline' }}", {}),
    # Variables, lists and loops, as rule templates use them.
    ("data_access", "{{ data.room }}", {"data": {"room": "lounge"}}),
    ("inputs_access", "{{ inputs['sensor.a'] }}", {"inputs": {"sensor.a": "on"}}),
    ("sort_first", "{{ [3, 1, 2] | sort | first }}", {}),
    (
        "selectattr",
        "{{ items | selectattr('on') | map(attribute='n') | list }}",
        {"items": [{"n": 1, "on": True}, {"n": 2, "on": False}]},
    ),
    (
        "namespace_loop",
        "{% set ns = namespace(n=0) %}{% for i in range(4) %}"
        "{% set ns.n = ns.n + i %}{% endfor %}{{ ns.n }}",
        {},
    ),
    (
        "decision_result",
        "{{ 'decline' if answers.cold.probability < 0.5 else 'heat' }}",
        {"answers": {"cold": {"probability": 0.8}}},
    ),
]


async def _render(
    ws: aiohttp.ClientWebSocketResponse,
    msg_id: int,
    template: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    await ws.send_json(
        {
            "id": msg_id,
            "type": "render_template",
            "template": template,
            "variables": variables,
            "report_errors": True,
            "timeout": 3,
        }
    )
    while True:
        msg = await ws.receive_json(timeout=10)
        if msg.get("id") != msg_id:
            continue
        if msg["type"] == "result":
            if not msg["success"]:
                return {"error": True}
            continue
        event = msg["event"]
        await ws.send_json(
            {"id": msg_id + 1, "type": "unsubscribe_events", "subscription": msg_id}
        )
        if "error" in event:
            return {"error": True}
        return {"result": event["result"]}


async def main() -> None:
    url = os.environ["HASS_URL"].rstrip("/")
    ws_url = url.replace("http", "ws", 1) + "/api/websocket"
    async with aiohttp.ClientSession() as session, session.ws_connect(ws_url) as ws:
        hello = await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": os.environ["HASS_TOKEN"]})
        if (await ws.receive_json())["type"] != "auth_ok":
            msg = "HA refused the token"
            raise SystemExit(msg)
        cases = []
        msg_id = 10
        for name, template, variables in CASES:
            outcome = await _render(ws, msg_id, template, variables)
            msg_id += 2
            cases.append(
                {"name": name, "template": template, "variables": variables, **outcome}
            )
    data = {
        "ha_version": hello.get("ha_version"),
        "recorded_at": datetime.now(UTC).date().isoformat(),
        "cases": cases,
    }
    path = Path(__file__).with_name("cases.json")
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
