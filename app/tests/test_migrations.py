import sqlite3
from pathlib import Path

import pytest
from hagent_app.storage import Database, MigrationError, connect, migrate
from hagent_app.storage.migrate import default_source, discover


def _write(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql)


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {r[0] for r in rows}


def _versions(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version")
    return [r[0] for r in rows]


def test_package_migrations_apply_and_are_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    assert migrate(conn) == _package_versions()
    assert "app_info" in _tables(conn)
    assert migrate(conn) == []
    assert _versions(conn) == _package_versions()


def test_package_migration_names_are_valid() -> None:
    migrations = discover(default_source())
    assert migrations[0].version == 1
    # Each area owns a range of 100 versions below 1000 (app/README.md).
    assert all(0 < m.version < 1000 for m in migrations)


def _package_versions() -> list[int]:
    return [m.version for m in discover(default_source())]


def test_numeric_order_and_late_lower_version(tmp_path: Path) -> None:
    src = tmp_path / "m"
    src.mkdir()
    _write(src, "0100_agents.sql", "CREATE TABLE agents (id INTEGER PRIMARY KEY);")
    _write(src, "0001_core.sql", "CREATE TABLE core (id INTEGER PRIMARY KEY);")
    _write(src, "README.txt", "ignored")
    conn = connect(":memory:")
    assert migrate(conn, src) == [1, 100]
    # A lower version merges after a higher one has run. It still applies.
    _write(src, "0002_more.sql", "CREATE TABLE more (id INTEGER PRIMARY KEY);")
    assert migrate(conn, src) == [2]
    assert _versions(conn) == [1, 2, 100]
    assert {"core", "agents", "more"} <= _tables(conn)


def test_failed_migration_leaves_nothing(tmp_path: Path) -> None:
    src = tmp_path / "m"
    src.mkdir()
    _write(src, "0001_core.sql", "CREATE TABLE core (id INTEGER PRIMARY KEY);")
    _write(
        src,
        "0002_bad.sql",
        "CREATE TABLE half (id INTEGER PRIMARY KEY);\nTHIS IS NOT SQL;",
    )
    conn = connect(":memory:")
    with pytest.raises(MigrationError, match="0002_bad"):
        migrate(conn, src)
    assert _versions(conn) == [1]
    assert "half" not in _tables(conn)
    assert not conn.in_transaction


@pytest.mark.parametrize(
    ("names", "match"),
    [
        (["0001_a.sql", "0001_b.sql"], "two migrations"),
        (["1_short.sql"], "bad migration file name"),
        (["0003_Upper.sql"], "bad migration file name"),
        (["0000_zero.sql"], "reserved"),
    ],
)
def test_bad_migration_files(tmp_path: Path, names: list[str], match: str) -> None:
    for name in names:
        _write(tmp_path, name, "SELECT 1;")
    with pytest.raises(MigrationError, match=match):
        migrate(connect(":memory:"), tmp_path)


def test_migrate_needs_autocommit() -> None:
    conn = sqlite3.connect(":memory:")
    with pytest.raises(MigrationError, match="autocommit"):
        migrate(conn)


async def test_database_open_runs_migrations(tmp_path: Path) -> None:
    path = tmp_path / "hagent.db"
    db = await Database.open(path)
    try:
        assert db.applied_migrations == _package_versions()
        mode = await db.run(lambda c: c.execute("PRAGMA journal_mode").fetchone()[0])
        fks = await db.run(lambda c: c.execute("PRAGMA foreign_keys").fetchone()[0])
        assert mode == "wal"
        assert fks == 1
    finally:
        await db.close()
    db = await Database.open(path)
    try:
        assert db.applied_migrations == []
    finally:
        await db.close()
