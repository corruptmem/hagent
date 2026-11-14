"""Configuration for the hagent app.

The HA Supervisor writes the app options to ``/data/options.json``. The app
reads that file first. Environment variables then override single values.
Use the overrides for development and for tests.

The core keys are in ``Config``. A later module does not add fields to
``Config``. It defines its own dataclass and reads its own section from
``Config.options`` with ``Config.section``. See ``app/README.md``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

SUPERVISOR_WS_URL = "ws://supervisor/core/websocket"
DEFAULT_DATA_DIR = Path("/data")
DEFAULT_PORT = 8099
OPTIONS_FILE = "options.json"
DATABASE_FILE = "hagent.db"
LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class ConfigError(ValueError):
    """The configuration is not valid."""


@dataclass(frozen=True, slots=True)
class Config:
    """The core configuration of the app.

    ``ha_url`` is the HA WebSocket API URL. Inside an app it is the
    Supervisor proxy. ``ha_token`` is the access token. Inside an app it is
    ``SUPERVISOR_TOKEN``. ``options`` holds every key of ``options.json``,
    also the keys that later modules own.
    """

    ha_url: str
    ha_token: str
    data_dir: Path
    port: int = DEFAULT_PORT
    log_level: str = "INFO"
    options: Mapping[str, Any] = field(default_factory=dict)

    @property
    def database_path(self) -> Path:
        """The path of the SQLite database file."""
        return self.data_dir / DATABASE_FILE

    def section[T](self, name: str, cls: type[T]) -> T:
        """Build a module config dataclass from the ``name`` section.

        The section is a JSON object in ``options.json``. Missing keys take
        the defaults of ``cls``. Unknown keys cause ``ConfigError``.
        """
        raw = self.options.get(name, {})
        if not isinstance(raw, Mapping):
            msg = f"option {name!r} must be an object"
            raise ConfigError(msg)
        known = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        unknown = set(raw) - known
        if unknown:
            msg = f"unknown keys in option {name!r}: {sorted(unknown)}"
            raise ConfigError(msg)
        try:
            return cls(**raw)
        except TypeError as err:
            msg = f"option {name!r} is not valid: {err}"
            raise ConfigError(msg) from err


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Load the configuration from ``options.json`` and the environment.

    Environment variables:

    - ``HAGENT_DATA_DIR``: the data directory. The default is ``/data``.
    - ``HAGENT_HA_URL``: the HA WebSocket URL. The default is the
      Supervisor proxy.
    - ``HAGENT_HA_TOKEN``: the access token. The default is
      ``SUPERVISOR_TOKEN``.
    - ``HAGENT_PORT``: the HTTP port. The default is 8099.
    - ``HAGENT_LOG_LEVEL``: the log level. The default is ``INFO``.
    """
    env = os.environ if env is None else env
    data_dir = Path(env.get("HAGENT_DATA_DIR", str(DEFAULT_DATA_DIR)))
    options = _read_options(data_dir / OPTIONS_FILE)

    token = env.get("HAGENT_HA_TOKEN") or env.get("SUPERVISOR_TOKEN", "")
    if not token:
        msg = "no HA token: set SUPERVISOR_TOKEN or HAGENT_HA_TOKEN"
        raise ConfigError(msg)

    port_text = env.get("HAGENT_PORT", str(options.get("port", DEFAULT_PORT)))
    try:
        port = int(port_text)
    except ValueError as err:
        msg = f"port is not an integer: {port_text!r}"
        raise ConfigError(msg) from err

    log_level = str(env.get("HAGENT_LOG_LEVEL", options.get("log_level", "INFO")))
    log_level = log_level.upper()
    if log_level not in LOG_LEVELS:
        msg = f"log level is not valid: {log_level!r}"
        raise ConfigError(msg)

    return Config(
        ha_url=env.get("HAGENT_HA_URL", SUPERVISOR_WS_URL),
        ha_token=token,
        data_dir=data_dir,
        port=port,
        log_level=log_level,
        options=options,
    )


def _read_options(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        msg = f"{path} is not valid JSON: {err}"
        raise ConfigError(msg) from err
    if not isinstance(data, dict):
        msg = f"{path} must hold a JSON object"
        raise ConfigError(msg)
    return data
