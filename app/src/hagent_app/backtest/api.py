"""The rule routes of the management API (``docs/api.md``, "Rules").

Every route under ``api/agents/{id}/rules`` reads or changes the rules of
one live agent only. Each SQL statement filters by the agent ID.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from hagent_app.backtest import store
from hagent_app.backtest.runner import BacktestError, BacktestOptions
from hagent_app.http.errors import ApiError, read_json
from hagent_app.rules.templates import TemplateError, check_template
from hagent_app.tools.stores import StoreError

if TYPE_CHECKING:
    from hagent_app.agents import Agent
    from hagent_app.backtest.component import BacktestComponent

_ID = "{agent_id:[a-f0-9]{32}}"
_RULE = "{rule_id:[a-f0-9]{32}}"
_VERSION = "{version:[0-9]{1,9}}"
_BACKTEST = "{backtest_id:[a-f0-9]{32}}"
_BASE = f"/api/agents/{_ID}/rules"

_STATUS = {
    "rule_not_found": 404,
    "version_not_found": 404,
    "invalid_rule": 422,
    "backtest_running": 409,
}


def _int(body: dict[str, Any], name: str, low: int, high: int) -> int | None:
    value = body.get(name)
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        msg = f"{name} must be an integer from {low} to {high}"
        raise ApiError(400, "invalid_body", msg)
    return value


async def _object(request: web.Request) -> dict[str, Any]:
    body = await read_json(request) if request.can_read_body else {}
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_body", "the body must be a JSON object")
    return body


class RulesApi:
    """Rules, versions, statistics, backtests, trials and the template check."""

    def __init__(self, component: Callable[[], BacktestComponent]) -> None:
        self._component = component

    def routes(self) -> list[web.RouteDef]:
        """The route table for ``HttpServer.add_routes``."""
        return [
            web.post("/api/rules/check-template", self.check),
            web.get(_BASE, self.list_rules),
            web.get(f"{_BASE}/{_RULE}", self.get_rule),
            web.delete(f"{_BASE}/{_RULE}", self.delete_rule),
            web.post(f"{_BASE}/{_RULE}/state", self.set_state),
            web.get(f"{_BASE}/{_RULE}/versions/{_VERSION}", self.get_version),
            web.post(f"{_BASE}/{_RULE}/rollback", self.rollback),
            web.get(f"{_BASE}/{_RULE}/backtests", self.list_backtests),
            web.post(f"{_BASE}/{_RULE}/backtests", self.backtest),
            web.get(f"{_BASE}/{_RULE}/backtests/{_BACKTEST}", self.get_backtest),
            web.get(f"{_BASE}/{_RULE}/trials", self.list_trials),
        ]

    # ------------------------------------------------------------ helpers

    async def _agent(self, request: web.Request) -> Agent:
        agent = await self._component().agent(request.match_info["agent_id"])
        if agent is None:
            raise ApiError(404, "agent_not_found", "the agent does not exist")
        return agent

    async def _rule(self, request: web.Request, agent: Agent) -> dict[str, Any]:
        component = self._component()
        rule_id = request.match_info["rule_id"]
        assert component.rules is not None  # noqa: S101 - set when running
        stats = await component.rules.store.rule_stats(agent.id, rule_id)
        if not stats:
            raise ApiError(404, "rule_not_found", "the agent has no rule with this ID")
        return stats

    # ------------------------------------------------------------ routes

    async def check(self, request: web.Request) -> web.Response:
        """Check one template against the supported subset."""
        body = await _object(request)
        template = body.get("template")
        if not isinstance(template, str) or not template:
            raise ApiError(400, "invalid_body", "template must be non-empty text")
        try:
            check_template(template)
        except TemplateError as err:
            return web.json_response({"ok": False, "error": str(err)})
        return web.json_response({"ok": True, "error": None})

    async def list_rules(self, request: web.Request) -> web.Response:
        """The rules of the agent in order, with the current spec."""
        agent = await self._agent(request)
        component = self._component()
        assert component.rules is not None  # noqa: S101 - set when running
        items = await component.rules.store.list_items(agent.id, "rule")
        return web.json_response({"rules": items})

    async def get_rule(self, request: web.Request) -> web.Response:
        """One rule: state, current spec and the statistics of each version."""
        agent = await self._agent(request)
        stats = await self._rule(request, agent)
        component = self._component()
        assert component.rules is not None  # noqa: S101 - set when running
        spec = await component.rules.store.get_spec(
            agent.id, stats["rule_id"], int(stats["current_version"])
        )
        return web.json_response({**stats, "id": stats["rule_id"], "spec": spec})

    async def get_version(self, request: web.Request) -> web.Response:
        """The spec of one version."""
        agent = await self._agent(request)
        await self._rule(request, agent)
        component = self._component()
        assert component.rules is not None  # noqa: S101 - set when running
        version = int(request.match_info["version"])
        rule_id = request.match_info["rule_id"]
        spec = await component.rules.store.get_spec(agent.id, rule_id, version)
        if spec is None:
            raise ApiError(
                404, "version_not_found", f"the rule has no version {version}"
            )
        return web.json_response({"rule_id": rule_id, "version": version, "spec": spec})

    async def rollback(self, request: web.Request) -> web.Response:
        """Make an older version the current version (design 10.6)."""
        agent = await self._agent(request)
        body = await _object(request)
        version = _int(body, "version", 1, 10**9)
        if version is None:
            raise ApiError(400, "invalid_body", "version is needed")
        component = self._component()
        assert component.rules is not None  # noqa: S101 - set when running
        rule_id = request.match_info["rule_id"]
        try:
            result = await component.rules.store.rollback(agent.id, rule_id, version)
        except StoreError as err:
            if err.code == "not_owner":
                raise ApiError(404, "rule_not_found", err.message) from err
            raise ApiError(404, "version_not_found", err.message) from err
        component.publish(
            "updated", rule_id, {"agent_id": agent.id, "version": version}
        )
        return web.json_response(result)

    async def set_state(self, request: web.Request) -> web.Response:
        """Turn a rule off, or turn an ``off`` rule on again (design 7.2).

        The user cannot activate a ``draft``: the agent activates a draft
        after its backtest (design 10.7).
        """
        agent = await self._agent(request)
        body = await _object(request)
        state = body.get("state")
        if state not in {"active", "off"}:
            raise ApiError(400, "invalid_body", "state must be 'active' or 'off'")
        stats = await self._rule(request, agent)
        rule_id = stats["rule_id"]
        if state == "active" and stats["state"] == "draft":
            msg = "the agent activates a draft rule after a backtest"
            raise ApiError(409, "rule_is_draft", msg)
        component = self._component()
        assert component.rules is not None  # noqa: S101 - set when running
        try:
            await component.rules.store.set_enabled(
                agent.id, "rule", rule_id, state == "active"
            )
        except StoreError as err:
            raise ApiError(404, "rule_not_found", err.message) from err
        version = int(stats["current_version"])
        component.publish(
            "updated", rule_id, {"agent_id": agent.id, "version": version}
        )
        return web.json_response({"id": rule_id, "version": version, "state": state})

    async def delete_rule(self, request: web.Request) -> web.Response:
        """Delete a rule. Its versions and statistics stay for history."""
        agent = await self._agent(request)
        stats = await self._rule(request, agent)
        rule_id = stats["rule_id"]
        component = self._component()
        assert component.rules is not None  # noqa: S101 - set when running
        try:
            await component.rules.store.delete(agent.id, "rule", rule_id)
        except StoreError as err:
            raise ApiError(404, "rule_not_found", err.message) from err
        component.publish("deleted", rule_id, {"agent_id": agent.id})
        return web.json_response({"deleted": rule_id})

    async def backtest(self, request: web.Request) -> web.Response:
        """Run one backtest and return it with its cases."""
        agent = await self._agent(request)
        body = await _object(request)
        component = self._component()
        live = body.get("live", True)
        if not isinstance(live, bool):
            raise ApiError(400, "invalid_body", "live must be true or false")
        options = BacktestOptions(
            version=_int(body, "version", 1, 10**9),
            days=_int(body, "days", 1, component.config.max_days),
            limit=_int(body, "limit", 1, component.config.max_cases),
            live=live,
        )
        try:
            result = await component.backtester.run(
                agent, request.match_info["rule_id"], options
            )
        except BacktestError as err:
            raise ApiError(_STATUS.get(err.code, 400), err.code, err.message) from err
        component.publish(
            "backtest_finished",
            result["rule_id"],
            {"agent_id": agent.id, "backtest_id": result["id"]},
        )
        return web.json_response(result)

    async def list_backtests(self, request: web.Request) -> web.Response:
        """The backtests of the rule, newest first, without cases."""
        agent = await self._agent(request)
        rule_id = request.match_info["rule_id"]

        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            return store.list_backtests(conn, agent.id, rule_id)

        return web.json_response({"backtests": await self._component().db_run(run)})

    async def get_backtest(self, request: web.Request) -> web.Response:
        """One backtest with its cases."""
        agent = await self._agent(request)
        backtest_id = request.match_info["backtest_id"]
        rule_id = request.match_info["rule_id"]

        def run(conn: sqlite3.Connection) -> dict[str, Any] | None:
            return store.get_backtest(conn, agent.id, backtest_id)

        found = await self._component().db_run(run)
        if found is None or found["rule_id"] != rule_id:
            raise ApiError(404, "backtest_not_found", "no backtest with this ID")
        return web.json_response(found)

    async def list_trials(self, request: web.Request) -> web.Response:
        """The trials of the rule, newest first."""
        agent = await self._agent(request)
        rule_id = request.match_info["rule_id"]

        def run(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            return store.list_trials(conn, agent.id, rule_id)

        return web.json_response({"trials": await self._component().db_run(run)})
