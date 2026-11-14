"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_MODEL = "claude-sonnet-5-5"


class ConfigError(Exception):
    """A required setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    ha_url: str
    ha_token: str
    model: str = DEFAULT_MODEL
    allow_actions: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        missing = [name for name in ("HA_URL", "HA_TOKEN") if not env.get(name)]
        if missing:
            raise ConfigError(f"Missing environment variables: {', '.join(missing)}")
        return cls(
            ha_url=env["HA_URL"].rstrip("/"),
            ha_token=env["HA_TOKEN"],
            model=env.get("HAGENT_MODEL") or DEFAULT_MODEL,
            allow_actions=env.get("HAGENT_ALLOW_ACTIONS", "").lower()
            in {"1", "true", "yes"},
        )
