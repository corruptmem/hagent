"""The SQLite database of the app.

The app uses the standard-library ``sqlite3`` module. One worker thread owns
the connection. Async code sends work to that thread with ``Database.run``,
so a slow query never blocks the event loop. Only one call runs at a time.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hagent_app.storage.migrate import migrate

BUSY_TIMEOUT_MS = 5000


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with the app settings.

    The connection uses ``autocommit=True``. Use ``with transaction(conn):``
    or explicit ``BEGIN`` and ``COMMIT`` to group writes. The settings are
    WAL journal mode (for a file database), foreign keys on, and a busy
    timeout.
    """
    conn = sqlite3.connect(path, autocommit=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


class transaction:  # noqa: N801 - used like a function: ``with transaction(c)``
    """Run a block in one write transaction. Roll back on an exception."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            self._conn.execute("COMMIT")
        else:
            self._conn.execute("ROLLBACK")


class Database:
    """An open database with one worker thread.

    Create it with ``await Database.open(path)``. The call runs the
    migrations before it returns.
    """

    def __init__(self, conn: sqlite3.Connection, executor: ThreadPoolExecutor) -> None:
        self._conn = conn
        self._executor = executor
        self.applied_migrations: list[int] = []

    @classmethod
    async def open(cls, path: Path | str) -> Database:
        """Open the database, apply the migrations, and return it."""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite")
        loop = asyncio.get_running_loop()
        try:
            conn = await loop.run_in_executor(executor, connect, path)
        except BaseException:
            executor.shutdown(wait=False)
            raise
        db = cls(conn, executor)
        try:
            db.applied_migrations = await db.run(migrate)
        except BaseException:
            await db.close()
            raise
        return db

    async def run[R](self, fn: Callable[[sqlite3.Connection], R]) -> R:
        """Run ``fn(connection)`` on the worker thread and return its result."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, self._conn)

    async def close(self) -> None:
        """Close the connection and stop the worker thread."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._conn.close)
        self._executor.shutdown(wait=True)
