"""The settings that the user changes in the management UI.

Design section 7.2 lists them. Each setting is optional. A setting that the
user did not set takes its default from the app options, so an existing
``options.json`` keeps working.

Terms:

- **Stored value**: the value in the ``app_settings`` table, or ``None``.
- **Effective value**: the stored value, or else the option default.

API keys are not part of ``SettingsPatch``. They have their own routes, and
the API never returns their value (see ``keys.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

USER_ID = re.compile(r"[0-9a-f]{32}")
MOBILE_SERVICE = re.compile(r"mobile_app_[a-z0-9_]+")
PRICE_KEY = re.compile(r"[a-z0-9_-]+:\S+")
MAX_APPROVAL_TIMEOUT = 3600.0

# The names of the settings in SettingsPatch, in display order.
SETTING_NAMES = (
    "installation_daily_budget_usd",
    "agent_daily_budget_usd",
    "price_overrides",
    "genai_prices_auto_update",
    "decision_models",
    "approvals",
)

Usd = Annotated[float, Field(ge=0, le=10000, allow_inf_nan=False)]
Rate = Annotated[float, Field(ge=0, le=100000, allow_inf_nan=False)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class PriceOverride(_Strict):
    """A price in USD per million tokens."""

    input: Rate
    output: Rate = 0.0
    cache_read: Rate | None = None
    cache_write: Rate | None = None


class DecisionModelEntry(_Strict):
    """An extra decision model."""

    provider: Literal["openai", "openrouter"]
    model: Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S+$")]


class ApprovalSettings(_Strict):
    """The approvers, their notify services and the time limit.

    The checks match ``ApprovalsConfig`` of the approvals area (task-16):

    - ``approvers``: HA user IDs (32 hexadecimal characters).
    - ``notify_services``: for an approver, an explicit list of mobile app
      notify services (``notify.mobile_app_<device>``). It replaces the
      default notify chain of that approver.
    - ``timeout_seconds``: how long an agent turn waits for an answer.
    """

    approvers: list[str] = Field(default_factory=list, max_length=50)
    notify_services: dict[str, list[str]] = Field(default_factory=dict)
    timeout_seconds: Annotated[
        float, Field(gt=0, le=MAX_APPROVAL_TIMEOUT, allow_inf_nan=False)
    ] = 300.0

    @model_validator(mode="after")
    def _check(self) -> ApprovalSettings:
        for user_id in self.approvers:
            if not USER_ID.fullmatch(user_id):
                msg = f"approvers: {user_id!r} is not an HA user ID"
                raise ValueError(msg)
        if len(set(self.approvers)) != len(self.approvers):
            msg = "approvers: each user can appear once"
            raise ValueError(msg)
        cleaned: dict[str, list[str]] = {}
        for user_id, services in self.notify_services.items():
            if user_id not in self.approvers:
                msg = f"notify_services: {user_id!r} is not an approver"
                raise ValueError(msg)
            if not services:
                msg = "notify_services: each list needs at least one service"
                raise ValueError(msg)
            names = []
            for service in services:
                name = service.removeprefix("notify.")
                if not MOBILE_SERVICE.fullmatch(name):
                    msg = (
                        f"notify_services: {service!r} is not a mobile app notify "
                        "service (notify.mobile_app_<device>)"
                    )
                    raise ValueError(msg)
                names.append(name)
            cleaned[user_id] = names
        object.__setattr__(self, "notify_services", cleaned)
        return self


class SettingsPatch(_Strict):
    """A change to the settings. A field set to ``null`` returns to the default."""

    installation_daily_budget_usd: Usd | None = None
    agent_daily_budget_usd: Usd | None = None
    price_overrides: dict[str, PriceOverride] | None = None
    genai_prices_auto_update: bool | None = None
    decision_models: list[DecisionModelEntry] | None = Field(
        default=None, max_length=100
    )
    approvals: ApprovalSettings | None = None

    @model_validator(mode="after")
    def _check(self) -> SettingsPatch:
        for key in self.price_overrides or {}:
            if not PRICE_KEY.fullmatch(key):
                msg = (
                    f"price_overrides: {key!r} must be '<price provider>:<model>', "
                    "for example 'openai:gpt-6'"
                )
                raise ValueError(msg)
        return self

    def changes(self) -> dict[str, Any]:
        """The fields that the request named, as JSON values (``None`` resets)."""
        data = self.model_dump(mode="json")
        return {name: data[name] for name in self.model_fields_set}


def parse_patch(body: Any) -> SettingsPatch:
    """Validate a change request. Raise ``ValidationError``."""
    return SettingsPatch.model_validate(body)


def validation_details(err: ValidationError) -> list[dict[str, Any]]:
    """The errors of ``err`` in the API format, without the input values."""
    return [
        {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
        for e in err.errors(include_url=False, include_input=False)
    ]


@dataclass(frozen=True, slots=True)
class Approvals:
    """The approval settings in the form that the approvals area reads."""

    approvers: tuple[str, ...]
    timeout_seconds: float
    notify_services: dict[str, list[str]] = field(default_factory=dict)
