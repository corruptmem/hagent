"""Smoke test for the task-1 spike image.

The test proves four facts inside the image:

- the interpreter is Python 3.14 or later;
- sqlite-vec loads into the standard-library SQLite;
- a vec0 table accepts inserts and returns KNN (k-nearest-neighbour) results;
- FTS5 (SQLite full-text search) is available in the same SQLite build.

The script exits with a non-zero status on the first failure.
"""

import sqlite3
import sys

import sqlite_vec

MIN_PYTHON = (3, 14)
KNN_LIMIT = 2

VECTORS: dict[int, list[float]] = {
    1: [1.0, 0.0, 0.0, 0.0],
    2: [0.0, 1.0, 0.0, 0.0],
    3: [0.9, 0.1, 0.0, 0.0],
}
QUERY = [1.0, 0.05, 0.0, 0.0]
EXPECTED_KNN = [1, 3]

DOCUMENTS = [
    (1, "The radiator in the back bedroom is cold."),
    (2, "The kitchen light turns on at sunset."),
]
FTS_QUERY = "radiator"
EXPECTED_FTS = [1]


def fail(message: str) -> None:
    """Stop the smoke test with a failure message."""
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def check_python() -> None:
    """Check the interpreter version."""
    print(f"python: {sys.version}")
    if sys.version_info < MIN_PYTHON:
        fail(f"Python {MIN_PYTHON} or later is necessary")


def open_database() -> sqlite3.Connection:
    """Open an in-memory database and load sqlite-vec into it."""
    conn = sqlite3.connect(":memory:")
    print(f"sqlite: {sqlite3.sqlite_version}")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    (version,) = conn.execute("SELECT vec_version()").fetchone()
    print(f"sqlite-vec: {version}")
    return conn


def check_vec0(conn: sqlite3.Connection) -> None:
    """Insert vectors into a vec0 table and run a KNN query."""
    conn.execute("CREATE VIRTUAL TABLE items USING vec0(embedding float[4])")
    conn.executemany(
        "INSERT INTO items(rowid, embedding) VALUES (?, ?)",
        [(rowid, sqlite_vec.serialize_float32(v)) for rowid, v in VECTORS.items()],
    )
    rows = conn.execute(
        "SELECT rowid, distance FROM items WHERE embedding MATCH ? AND k = ? "
        "ORDER BY distance",
        (sqlite_vec.serialize_float32(QUERY), KNN_LIMIT),
    ).fetchall()
    print(f"vec0 knn: {rows}")
    if [rowid for rowid, _ in rows] != EXPECTED_KNN:
        fail(f"KNN returned {rows}, expected row IDs {EXPECTED_KNN}")


def check_fts5(conn: sqlite3.Connection) -> None:
    """Create an FTS5 table and run a full-text query."""
    conn.execute("CREATE VIRTUAL TABLE docs USING fts5(body)")
    conn.executemany("INSERT INTO docs(rowid, body) VALUES (?, ?)", DOCUMENTS)
    rows = conn.execute(
        "SELECT rowid FROM docs WHERE docs MATCH ? ORDER BY rank", (FTS_QUERY,)
    ).fetchall()
    print(f"fts5 match: {rows}")
    if [rowid for (rowid,) in rows] != EXPECTED_FTS:
        fail(f"FTS5 returned {rows}, expected row IDs {EXPECTED_FTS}")


def main() -> None:
    """Run all checks."""
    check_python()
    conn = open_database()
    try:
        check_vec0(conn)
        check_fts5(conn)
    finally:
        conn.close()
    print("PASS")


if __name__ == "__main__":
    main()
