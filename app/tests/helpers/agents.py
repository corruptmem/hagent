"""Helpers for the agents tests."""

from __future__ import annotations

import copy
from typing import Any


def definition(**changes: Any) -> dict[str, Any]:
    """A valid definition as JSON data, with changes applied.

    A key uses ``__`` to name a nested field, for example
    ``tools__ha__level="read"``.
    """
    data: dict[str, Any] = {
        "name": "Heating",
        "instructions": "Keep the rooms warm.",
        "models": {"chat": {"provider": "openai", "model": "gpt-5-mini"}},
    }
    data = copy.deepcopy(data)
    for key, value in changes.items():
        target = data
        *parents, last = key.split("__")
        for part in parents:
            target = target.setdefault(part, {})
        target[last] = value
    return data
