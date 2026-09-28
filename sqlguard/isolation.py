"""State isolation: read-only schema access over a local SQLite fixture.

Two guarantees matter for a reviewer that *does not execute user SQL*:

1. The process must never be able to mutate the fixture. Every connection is
   opened read-only and immutable via a ``file:...?mode=ro&immutable=1`` URI
   (the file is a shipped fixture, not an operational database), and SQL
   submitted for review is never passed to ``execute`` at all.
2. Analysis uses an in-memory **schema snapshot** (table/column names taken
   from ``PRAGMA``), so a review is reproducible against one fixed catalog
   state and reports the catalog digest it reasoned over.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path


class SchemaUnavailable(Exception):
    pass


@dataclass(frozen=True)
class SchemaSnapshot:
    path: str
    tables: dict[str, tuple[str, ...]]
    digest: str

    def has_table(self, name: str) -> bool:
        return name.lower() in self.tables

    def has_column(self, table: str, column: str) -> bool:
        cols = self.tables.get(table.lower())
        return bool(cols and column.lower() in cols)


def open_readonly_connection(path: str | Path) -> sqlite3.Connection:
    """Open a connection that physically cannot write the database."""
    p = Path(path).resolve()
    if not p.exists():
        raise SchemaUnavailable(f"fixture database not found: {p}")
    uri = f"file:{p}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    # Defense in depth: refuse anything other than SELECT/PRAGMA on this
    # connection. The analyzer never calls execute() on user SQL anyway.
    conn.set_authorizer(_readonly_authorizer)
    return conn


def _readonly_authorizer(action, arg1, arg2, db_name, trigger):
    # Only allow read-ish actions; deny writes/attach/schema changes.
    allowed = {
        sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
        sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_FUNCTION,
        getattr(sqlite3, "SQLITE_ANALYZE", 0),
    }
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def snapshot_schema(path: str | Path) -> SchemaSnapshot:
    """Read the catalog through a read-only connection, then close it."""
    conn = open_readonly_connection(path)
    try:
        tables: dict[str, tuple[str, ...]] = {}
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ):
            cols = tuple(
                row[1].lower()
                for row in conn.execute(f'PRAGMA table_info("{name}")')
            )
            tables[name.lower()] = cols
    except sqlite3.DatabaseError as exc:
        raise SchemaUnavailable(str(exc)) from exc
    finally:
        conn.close()
    basis = "\n".join(f"{t}:{','.join(cs)}" for t, cs in tables.items())
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return SchemaSnapshot(path=str(Path(path).resolve()), tables=tables,
                          digest=digest)
