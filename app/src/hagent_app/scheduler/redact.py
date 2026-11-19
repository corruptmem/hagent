"""Remove approval secrets from event data before an agent sees it.

An approval notification carries action IDs (``HAGENT_ACCEPT_<id>`` and
``HAGENT_REJECT_<id>``) and a tag (``hagent-approval-<id>``). An agent that
saw an action ID could answer its own approval request (task-16). No agent
can subscribe to ``call_service`` events, but other events, for example the
action event or a state attribute, can carry these strings too. This is a
second barrier: the scheduler redacts every delivered payload.
"""

from __future__ import annotations

import re
from typing import Any

from hagent_app.approvals.service import ACTION_ID

APPROVAL_TAG = re.compile(r"hagent-approval-[0-9a-f]{32}")
REDACTED = "[redacted]"


def redact_text(text: str) -> str:
    """The text with each action ID and approval tag replaced."""
    return APPROVAL_TAG.sub(REDACTED, ACTION_ID.sub(REDACTED, text))


def redact(value: Any) -> Any:
    """A copy of JSON data with each action ID and approval tag replaced.

    The function checks keys and values, at every depth.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {redact_text(str(k)): redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def contains_secret(value: Any) -> bool:
    """True if JSON data holds an action ID or an approval tag."""
    if isinstance(value, str):
        return bool(ACTION_ID.search(value) or APPROVAL_TAG.search(value))
    if isinstance(value, dict):
        return any(
            contains_secret(str(k)) or contains_secret(v) for k, v in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_secret(v) for v in value)
    return False
