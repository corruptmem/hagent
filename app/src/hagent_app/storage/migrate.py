"""SQLite schema migrations.

A migration is an SQL file with the name ``NNNN_<name>.sql``. ``NNNN`` is a
four-digit version number. Each area of the app owns a range of numbers (see
``app/README.md``). The ranges do not set the merge order. Version 0100 can
arrive before version 0002. The runner therefore applies every version that
is not in ``schema_migrations``, in numeric order. It does not apply only the
versions above the highest applied version.

The runner applies each file and its ``schema_migrations`` row in one
transaction. A failed file leaves no tables and no row. A migration file must
not contain ``BEGIN``, ``COMMIT`` or ``ROLLBACK``.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from importlib.resources.abc import Traversable

_LOG = logging.getLogger(__name__)
_NAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")

MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
) STRICT
"""


class MigrationError(RuntimeError):
    """A migration file is not valid, or it failed."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One migration file."""

    version: int
    name: str
    sql: str


def default_source() -> Traversable:
    """The migration directory inside the package."""
    return files("hagent_app.storage") / "migrations"


def discover(source: Traversable) -> list[Migration]:
    """Read the migration files in ``source``, sorted by version.

    Files that do not end in ``.sql`` are ignored. An ``.sql`` file with a bad
    name, or two files with the same version, cause ``MigrationError``.
    """
    found: dict[int, Migration] = {}
    for entry in source.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        match = _NAME.match(entry.name)
        if match is None:
            msg = f"bad migration file name: {entry.name}"
            raise MigrationError(msg)
        version = int(match["version"])
        if version == 0:
            msg = f"migration version 0000 is reserved: {entry.name}"
            raise MigrationError(msg)
        if version in found:
            msg = (
                f"two migrations use version {version:04d}: "
                f"{found[version].name} and {match['name']}"
            )
            raise MigrationError(msg)
        found[version] = Migration(
            version, match["name"], entry.read_text(encoding="utf-8")
        )
    return [found[v] for v in sorted(found)]


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """The versions in ``schema_migrations``."""
    conn.execute(MIGRATIONS_TABLE)
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def migrate(conn: sqlite3.Connection, source: Traversable | None = None) -> list[int]:
    """Apply every migration that has not run. Return the applied versions.

    ``conn`` must use ``autocommit=True``, so that the runner controls each
    transaction.
    """
    if conn.autocommit is not True:
        msg = "migrate needs a connection with autocommit=True"
        raise MigrationError(msg)
    migrations = discover(source if source is not None else default_source())
    done = applied_versions(conn)
    applied: list[int] = []
    for migration in migrations:
        if migration.version in done:
            continue
        _apply(conn, migration)
        applied.append(migration.version)
    return applied


def _apply(conn: sqlite3.Connection, migration: Migration) -> None:
    label = f"{migration.version:04d}_{migration.name}"
    _LOG.info("applying migration %s", label)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executescript(migration.sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (?, ?, ?)",
            (migration.version, migration.name, datetime.now(UTC).isoformat()),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as err:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        msg = f"migration {label} failed: {err}"
        raise MigrationError(msg) from err
