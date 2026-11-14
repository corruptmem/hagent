"""Tools that the LLM can call, and the code that runs them."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from hagent.ha import HomeAssistantClient, HomeAssistantError

# Returns True if the user approves the service call.
ConfirmFn = Callable[[str, str, dict[str, Any]], bool]

READ_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_entities",
        "description": (
            "List Home Assistant entities with their current state and friendly "
            "name. Filter by domain (for example 'light', 'climate', 'sensor') "
            "to keep the result small."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Entity domain prefix, without the dot.",
                }
            },
        },
    },
    {
        "name": "get_state",
        "description": "Get the full state and attributes of one entity.",
        "input_schema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        },
    },
    {
        "name": "list_services",
        "description": "List the services that one domain provides, with fields.",
        "input_schema": {
            "type": "object",
            "properties": {"domain": {"type": "string"}},
            "required": ["domain"],
        },
    },
]

ACTION_TOOLS: list[dict[str, Any]] = [
    {
        "name": "call_service",
        "description": (
            "Call a Home Assistant service, for example light.turn_on. This "
            "changes physical devices. The user must approve each call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "service": {"type": "string"},
                "data": {
                    "type": "object",
                    "description": "Service data, for example {'entity_id': ...}.",
                },
            },
            "required": ["domain", "service"],
        },
    },
]


def deny_all(_domain: str, _service: str, _data: dict[str, Any]) -> bool:
    return False


class Toolbox:
    """Runs tool calls against Home Assistant."""

    def __init__(
        self,
        ha: HomeAssistantClient,
        *,
        allow_actions: bool = False,
        confirm: ConfirmFn = deny_all,
    ) -> None:
        self._ha = ha
        self._allow_actions = allow_actions
        self._confirm = confirm

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return READ_TOOLS + (ACTION_TOOLS if self._allow_actions else [])

    def run(self, name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Run one tool. Return (content, is_error)."""
        try:
            return json.dumps(self._dispatch(name, tool_input), default=str), False
        except (HomeAssistantError, KeyError, ValueError) as err:
            return f"{type(err).__name__}: {err}", True

    def _dispatch(self, name: str, args: dict[str, Any]) -> Any:
        if name == "list_entities":
            domain = args.get("domain")
            return [
                {
                    "entity_id": s["entity_id"],
                    "state": s["state"],
                    "name": s.get("attributes", {}).get("friendly_name"),
                }
                for s in self._ha.list_states()
                if not domain or s["entity_id"].startswith(f"{domain}.")
            ]
        if name == "get_state":
            return self._ha.get_state(args["entity_id"])
        if name == "list_services":
            for entry in self._ha.list_services():
                if entry["domain"] == args["domain"]:
                    return entry["services"]
            raise ValueError(f"Unknown domain: {args['domain']}")
        if name == "call_service" and self._allow_actions:
            domain, service = args["domain"], args["service"]
            data = args.get("data") or {}
            if not self._confirm(domain, service, data):
                return {"status": "rejected", "reason": "The user did not approve."}
            changed = self._ha.call_service(domain, service, data)
            return {"status": "done", "changed": [s["entity_id"] for s in changed]}
        raise ValueError(f"Unknown or disabled tool: {name}")
