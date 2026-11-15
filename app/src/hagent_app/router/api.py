"""The test invocation route (``docs/api.md``, "Test invocation")."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from aiohttp import web

from hagent_app.agents.models import AgentError
from hagent_app.agents.store import parse_definition
from hagent_app.http.errors import ApiError, read_json
from hagent_app.router.errors import RouterNotRunningError

if TYPE_CHECKING:
    from hagent_app.router.component import RouterComponent, TestOutcome
    from hagent_app.turns.models import ToolCallRecord

_ID = "{agent_id:[a-f0-9]{32}}"
_STATUS = {"agent_not_found": 404, "definition_invalid": 422}


class RouterApi:
    """``POST api/agents/{id}/test``."""

    def __init__(self, router: Callable[[], RouterComponent]) -> None:
        self._router = router

    def routes(self) -> list[web.RouteDef]:
        """The route table for ``HttpServer.add_routes``."""
        return [web.post(f"/api/agents/{_ID}/test", self.test)]

    async def test(self, request: web.Request) -> web.Response:
        """Run one dry-run agent turn with sample data."""
        body = await read_json(request)
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_body", "the body must be a JSON object")
        definition = None
        raw = body.get("definition")
        if raw is not None:
            if not isinstance(raw, dict):
                raise ApiError(400, "invalid_body", "definition must be an object")
            try:
                definition = parse_definition(json.dumps(raw))
            except AgentError as err:
                raise ApiError(422, err.code, err.message, err.details) from err
        text, data = _input(body.get("input"))
        states = _states(body.get("states"))
        router = self._router()
        try:
            outcome = await router.test(
                request.match_info["agent_id"],
                definition=definition,
                text=text,
                data=data,
                states=states,
            )
        except RouterNotRunningError as err:
            raise ApiError(503, "not_ready", str(err)) from err
        except AgentError as err:
            status = _STATUS.get(err.code, 400)
            raise ApiError(status, err.code, err.message, err.details) from err
        return web.json_response(test_json(outcome))


def _input(value: Any) -> tuple[str | None, dict[str, Any]]:
    if value is None:
        return None, {}
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_body", "input must be an object")
    text = value.get("text")
    if text is not None and not isinstance(text, str):
        raise ApiError(400, "invalid_body", "input.text must be text")
    data = value.get("data")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_body", "input.data must be an object")
    return text, data


def _states(value: Any) -> dict[str, Mapping[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_body", "states must be an object")
    for entity_id, state in value.items():
        if not isinstance(state, dict) or not isinstance(state.get("state", ""), str):
            raise ApiError(
                400,
                "invalid_body",
                f"states.{entity_id} needs a text 'state' and an 'attributes' object",
            )
        if not isinstance(state.get("attributes") or {}, dict):
            raise ApiError(400, "invalid_body", f"states.{entity_id}.attributes")
    return value


def _planned(call: ToolCallRecord) -> dict[str, Any] | None:
    result = call.result if isinstance(call.result, dict) else {}
    data = result.get("data") if result.get("ok") else None
    if not isinstance(data, dict) or not data.get("dry_run"):
        return None
    planned = dict(data.get("request") or {})
    if call.tool != "ha.call_service":
        planned["tool"] = call.tool
    planned["needs_approval"] = bool(data.get("needs_approval"))
    return planned


def _denied(call: ToolCallRecord) -> dict[str, Any] | None:
    if call.ok:
        return None
    result = call.result if isinstance(call.result, dict) else {}
    raw = result.get("error")
    error: dict[str, Any] = raw if isinstance(raw, dict) else {}
    denied = dict(call.arguments) if isinstance(call.arguments, dict) else {}
    denied["tool"] = call.tool
    denied["code"] = call.error_code
    denied["reason"] = error.get("message") or call.error_code
    return denied


def test_json(outcome: TestOutcome) -> dict[str, Any]:
    """The response body of a test invocation."""
    result = outcome.result
    calls = () if outcome.record is None else outcome.record.tool_call_records
    usage = None if outcome.turn is None else outcome.turn.usage
    body: dict[str, Any] = {
        "invocation_id": result.invocation_id,
        "status": result.status,
        "path": result.path,
        "result": result.result,
        "planned_calls": [p for c in calls if (p := _planned(c)) is not None],
        "denied_calls": [d for c in calls if (d := _denied(c)) is not None],
        "tool_calls": 0 if usage is None else usage.tool_calls,
        "tokens": 0 if usage is None else usage.input_tokens + usage.output_tokens,
        "cost_usd": float(result.cost_usd),
    }
    if result.fallback_reason is not None:
        body["fallback_reason"] = result.fallback_reason
    if result.error_code is not None:
        body["error"] = {"code": result.error_code, "message": result.error}
    return body
