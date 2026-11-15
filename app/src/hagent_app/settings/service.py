"""The settings store.

``SettingsService`` keeps the rows of ``app_settings`` in memory. Reads are
plain method calls, so that a provider call can read a key without a
database round trip. A change writes the database first, then the memory
copy, then tells the listeners.

API keys are rows with the key ``api_key.<provider>``. ``key`` returns them
as a ``Secret``. No method returns a key as plain text, and no log line
holds one.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from hagent_app.providers.config import PROVIDERS, Provider, Secret
from hagent_app.settings.model import SETTING_NAMES, Approvals
from hagent_app.storage import transaction

if TYPE_CHECKING:
    from hagent_app.storage import Database

_LOG = logging.getLogger(__name__)
_KEY_PREFIX = "api_key."

type Listener = Callable[[frozenset[str]], None]


class SettingsService:
    """The stored settings and the API keys. See the module docstring."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._values: dict[str, Any] = {}
        self._keys: dict[str, Secret] = {}
        self._listeners: list[Listener] = []

    async def load(self) -> None:
        """Read every row into memory."""
        rows = await self._db.run(
            lambda c: c.execute("SELECT key, value FROM app_settings").fetchall()
        )
        values: dict[str, Any] = {}
        keys: dict[str, Secret] = {}
        for name, raw in rows:
            if name.startswith(_KEY_PREFIX):
                provider = name.removeprefix(_KEY_PREFIX)
                if provider in PROVIDERS:
                    keys[provider] = Secret(json.loads(raw))
            elif name in SETTING_NAMES:
                values[name] = json.loads(raw)
            else:
                _LOG.warning("ignoring unknown setting %r", name)
        self._values = values
        self._keys = keys

    # Reads

    def stored(self, name: str) -> Any:
        """The stored value of a setting, or ``None``."""
        return self._values.get(name)

    def stored_values(self) -> dict[str, Any]:
        """Every setting name with its stored value or ``None``."""
        return {name: self._values.get(name) for name in SETTING_NAMES}

    def key(self, provider: Provider) -> Secret | None:
        """The stored API key of ``provider``, or ``None``."""
        return self._keys.get(provider)

    def approvals(self) -> Approvals | None:
        """The approval settings, or ``None`` when the user set none.

        The approvals area falls back to its ``approvals`` option section
        when this returns ``None``.
        """
        raw = self._values.get("approvals")
        if raw is None:
            return None
        return Approvals(
            approvers=tuple(raw["approvers"]),
            timeout_seconds=float(raw["timeout_seconds"]),
            notify_services={k: list(v) for k, v in raw["notify_services"].items()},
        )

    # Changes

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener`` with the changed names after each change.

        The listener is a plain function. It runs on the event loop and must
        be fast. Return a function that removes the listener.
        """
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    async def update(self, changes: Mapping[str, Any]) -> None:
        """Store validated values. ``None`` deletes the row (back to default)."""
        unknown = set(changes) - set(SETTING_NAMES)
        if unknown:
            msg = f"unknown settings: {sorted(unknown)}"
            raise ValueError(msg)
        await self._write(dict(changes))
        for name, value in changes.items():
            if value is None:
                self._values.pop(name, None)
            else:
                self._values[name] = value
        self._notify(frozenset(changes))

    async def set_key(self, provider: Provider, value: str | None) -> None:
        """Store or delete the API key of ``provider``."""
        await self._write({_KEY_PREFIX + provider: value})
        if value is None:
            self._keys.pop(provider, None)
        else:
            self._keys[provider] = Secret(value)
        self._notify(frozenset({_KEY_PREFIX + provider}))

    async def _write(self, rows: Mapping[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()

        def run(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                for name, value in rows.items():
                    if value is None:
                        conn.execute("DELETE FROM app_settings WHERE key = ?", (name,))
                    else:
                        conn.execute(
                            "INSERT INTO app_settings (key, value, updated_at) "
                            "VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET "
                            "value = excluded.value, updated_at = excluded.updated_at",
                            (name, json.dumps(value), now),
                        )

        await self._db.run(run)

    def _notify(self, names: frozenset[str]) -> None:
        for listener in list(self._listeners):
            try:
                listener(names)
            except Exception:
                _LOG.exception("settings listener failed")
