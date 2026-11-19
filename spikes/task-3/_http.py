"""Small stdlib HTTP helper for the task-3 probes.

The probes read API keys from environment variables only. They never print
the keys.
"""

import json
import os
import urllib.request
from typing import Any

OPENAI_BASE = "https://api.openai.com/v1"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def _key(provider: str) -> str:
    name = "OPENAI_API_KEY" if provider == "openai" else "OPENROUTER_API_KEY"
    return os.environ[name]


def _base(provider: str) -> str:
    return OPENAI_BASE if provider == "openai" else OPENROUTER_BASE


def call(
    provider: str, path: str, body: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Send a GET (no body) or POST (body) request. Return JSON and headers."""
    url = path if path.startswith("https://") else _base(provider) + path
    if not url.startswith("https://"):
        raise ValueError(url)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data)  # noqa: S310 - fixed https URL
    req.add_header("Authorization", f"Bearer {_key(provider)}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            return json.loads(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as err:
        return {"http_error": err.code, "body": err.read().decode()[:2000]}, {}


def dump(label: str, obj: Any) -> None:
    print(f"== {label}")
    print(json.dumps(obj, indent=2, sort_keys=True))
