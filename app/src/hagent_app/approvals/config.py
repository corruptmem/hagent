"""The ``approvals`` option section.

Design section 7.2 puts the approvers in Settings. Until the settings store
exists (task-20), the app reads them from the ``approvals`` section of
``options.json``:

```json
{"approvals": {
    "approvers": ["<HA user ID>"],
    "timeout_seconds": 300,
    "notify_services": {"<HA user ID>": ["mobile_app_<device>"]}
}}
```

- ``approvers``: the HA user IDs that can accept or reject a call.
- ``timeout_seconds``: how long an agent turn waits for an answer.
- ``notify_services``: an optional explicit notify service list for an
  approver. It replaces the person chain for that approver (see
  ``notifier.py``).
- ``dev_trust_user_header``: development only. The API then trusts the
  ``X-Remote-User-Id`` header from every allowed peer, not only from the
  Supervisor address. The app logs a warning at start.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from hagent_app.config import ConfigError

USER_ID = re.compile(r"[0-9a-f]{32}")
MOBILE_SERVICE = re.compile(r"mobile_app_[a-z0-9_]+")
MAX_TIMEOUT = 3600.0


def _service_name(value: object) -> str:
    if not isinstance(value, str):
        msg = "approvals.notify_services values must be lists of text"
        raise ConfigError(msg)
    name = value.removeprefix("notify.")
    if not MOBILE_SERVICE.fullmatch(name):
        msg = (
            f"approvals.notify_services: {value!r} is not a mobile app notify "
            "service (notify.mobile_app_<device>)"
        )
        raise ConfigError(msg)
    return name


@dataclass(frozen=True)
class ApprovalsConfig:
    """The approval settings. ``__post_init__`` checks every value."""

    approvers: list[str] = field(default_factory=list)
    timeout_seconds: float = 300.0
    notify_services: dict[str, list[str]] = field(default_factory=dict)
    # Development only: trust X-Remote-User-Id from every allowed peer, not
    # only from the Supervisor. The app logs a warning at start.
    dev_trust_user_header: bool = False

    def __post_init__(self) -> None:
        flag: object = self.dev_trust_user_header
        if not isinstance(flag, bool):
            msg = "approvals.dev_trust_user_header must be true or false"
            raise ConfigError(msg)
        if not isinstance(self.approvers, list) or not all(
            isinstance(a, str) and USER_ID.fullmatch(a) for a in self.approvers
        ):
            msg = "approvals.approvers must be a list of HA user IDs"
            raise ConfigError(msg)
        timeout = self.timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not 0 < timeout <= MAX_TIMEOUT
        ):
            msg = f"approvals.timeout_seconds must be above 0 and at most {MAX_TIMEOUT}"
            raise ConfigError(msg)
        raw: object = self.notify_services
        if not isinstance(raw, dict):
            msg = "approvals.notify_services must be an object"
            raise ConfigError(msg)
        cleaned: dict[str, list[str]] = {}
        for user_id, services in raw.items():
            if user_id not in self.approvers:
                msg = f"approvals.notify_services: {user_id!r} is not an approver"
                raise ConfigError(msg)
            if not isinstance(services, list) or not services:
                msg = "approvals.notify_services values must be non-empty lists"
                raise ConfigError(msg)
            cleaned[user_id] = [_service_name(s) for s in services]
        object.__setattr__(self, "notify_services", cleaned)
        object.__setattr__(self, "approvers", list(dict.fromkeys(self.approvers)))
