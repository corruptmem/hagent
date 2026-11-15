"""The rule stage of the router (design sections 9.4 and 10).

The router asks a ``RuleStage`` before it starts an agent turn. Task-13
supplies the real stage. Until then, ``NoRules`` answers "no rule matched"
for every invocation, and the router starts an agent turn.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from hagent_app.agents.models import Agent
from hagent_app.router.models import InvocationRequest


@dataclass(frozen=True, slots=True)
class RuleRequest:
    """What the rule stage sees of one invocation.

    ``agent`` is the snapshot that the invocation keeps. ``inputs`` are the
    collected input values.
    """

    invocation_id: str
    agent: Agent
    request: InvocationRequest
    inputs: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """The answer of the rule stage.

    - ``answered``: true if a rule answered the invocation.
    - ``rule_id`` and ``rule_version``: the rule that answered, or the rule
      that declined or failed.
    - ``rules_version``: the version of the rule set that the stage used.
      The stage reads it once, at the start of the invocation, so that a
      rule change takes effect only between invocations (design 9.6).
    - ``reason``: why no rule answered, for example ``no_rule_matched``,
      ``declined`` or ``rule_error``.
    """

    answered: bool
    result: Any = None
    rule_id: str | None = None
    rule_version: int | None = None
    rules_version: int | None = None
    reason: str | None = None


NO_RULE_MATCHED = RuleOutcome(answered=False, reason="no_rule_matched")


class RuleStage(Protocol):
    """Try the active rules of an agent, in order (task-13)."""

    async def try_rules(self, request: RuleRequest) -> RuleOutcome:
        """Return the outcome. A rule that fails returns ``answered=False``."""
        ...


class NoRules:
    """The rule stage until task-13: no rule ever matches."""

    async def try_rules(self, request: RuleRequest) -> RuleOutcome:
        """Return "no rule matched"."""
        return NO_RULE_MATCHED
