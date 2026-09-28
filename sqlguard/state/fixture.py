"""Read-only SQLite fixture access.

The reviewer never executes submitted SQL (see kernel.py). It only opens a
*local synthetic* database in immutable read-only mode and reads its catalog
(``sqlite_master`` / ``PRAGMA table_info``) so that static and dynamic
identifiers can be checked against real schema.

The file is opened with the ``file:...?mode=ro&immutable=1`` URI. Any write
attempt raises ``sqlite3.OperationalError`` — used directly by the isolation
tests.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA_FILENAME = "schema.sql"
SEED_FILENAME = "seed.sql"


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    notnull: bool
    pk: int


# Authorizer action constants (SQLITE_CORE level, stable across SQLite versions).
_SQLITE_DENY = 1
_SQLITE_IGNORE = 2
# sqlite3 module action codes
_ATTACH = getattr(sqlite3, "SQLITE_ATTACH", None)
_DETACH = getattr(sqlite3, "SQLITE_DETACH", None)
_PRAGMA = getattr(sqlite3, "SQLITE_PRAGMA", None)
_WRITE_ACTIONS = {
    getattr(sqlite3, name, None) for name in (
        "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE",
        "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE",
        "SQLITE_CREATE_TEMP_INDEX", "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_CREATE_TEMP_VIEW",
        "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW", "SQLITE_CREATE_VTABLE",
        "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TEMP_INDEX", "SQLITE_DROP_TEMP_TABLE",
        "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW",
        "SQLITE_DROP_TRIGGER", "SQLITE_DROP_VIEW", "SQLITE_DROP_VTABLE",
        "SQLITE_ALTER_TABLE", "SQLITE_REINDEX", "SQLITE_ANALYZE",
        "SQLITE_TRANSACTION",
    )
}
_WRITE_ACTIONS.discard(None)
# PRAGMAs safe for read-only catalog inspection
_SAFE_PRAGMAS = {"table_info", "table_xinfo", "database_list",
                 "table_list", "function_list", "module_list"}


def _read_only_authorizer(action, arg1, arg2, db_name, trigger):
    """Defense in depth on top of the ``mode=ro&immutable=1`` mount.

    * ATTACH/DETACH are denied outright so a re-opened ``?mode=rw`` URI can
      never smuggle a writable handle into the connection;
    * PRAGMA is whitelisted to a handful of read-only catalog introspectors
      (``journal_mode`` and friends are denied);
    * write actions return IGNORE (not DENY) so that ``EXPLAIN QUERY PLAN``
      against DML — which invokes INSERT/UPDATE/DELETE authorizer callbacks at
      prepare time — can still produce a plan. Actual execution of writes is
      independently impossible because the database is mounted immutable:
      the mount itself raises "attempt to write a readonly database".
    """
    if _ATTACH is not None and action == _ATTACH:
        return _SQLITE_DENY
    if _DETACH is not None and action == _DETACH:
        return _SQLITE_DENY
    if action in _WRITE_ACTIONS:
        return _SQLITE_IGNORE
    if _PRAGMA is not None and action == _PRAGMA:
        if (arg1 or "").lower() in _SAFE_PRAGMAS:
            return sqlite3.SQLITE_OK
        return _SQLITE_DENY
    return sqlite3.SQLITE_OK


class ReadOnlyFixture:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        uri = f"file:{self.path.resolve()}?mode=ro&immutable=1"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.set_authorizer(_read_only_authorizer)
        self._tables: dict[str, list[ColumnInfo]] | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        """Raw read-only connection. Only catalog/SELECT use is permitted."""
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ReadOnlyFixture":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def cursor(self):
        cur = self._conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    def tables(self) -> dict[str, list[ColumnInfo]]:
        if self._tables is None:
            tables: dict[str, list[ColumnInfo]] = {}
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ):
                name = row[0]
                cols = [
                    ColumnInfo(r[1], (r[2] or "").upper(), bool(r[3]), int(r[5] or 0))
                    for r in self._conn.execute(f'PRAGMA table_info("{name}")')
                ]
                tables[name] = cols
            self._tables = tables
        return self._tables

    def has_table(self, name: str) -> bool:
        # SQLite identifier resolution is case-insensitive (ASCII).
        return name.casefold() in {n.casefold() for n in self.tables()}

    def has_column(self, table: str, column: str) -> bool:
        folded = {n.casefold(): n for n in self.tables()}
        real_table = folded.get(table.casefold(), table)
        cols = self.tables().get(real_table, [])
        return any(c.name.casefold() == column.casefold() for c in cols)

    def columns(self, table: str) -> list[ColumnInfo]:
        return self.tables().get(table, [])


def create_fixture(db_path: str | Path, schema_sql: str, seed_sql: str | None = None) -> Path:
    """Create a fresh fixture DB file (used by scripts/init_fixture.py and tests).

    Creation happens on a normal read-write connection; review time opens the
    same file strictly read-only.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(schema_sql)
        if seed_sql:
            conn.executescript(seed_sql)
        conn.commit()
    finally:
        conn.close()
    return path


def fixture_from_dir(dir_path: str | Path) -> ReadOnlyFixture:
    d = Path(dir_path)
    db_path = d / "fixture.db"
    if not db_path.exists():
        schema = (d / SCHEMA_FILENAME).read_text(encoding="utf-8")
        seed_path = d / SEED_FILENAME
        seed = seed_path.read_text(encoding="utf-8") if seed_path.exists() else None
        create_fixture(db_path, schema, seed)
    return ReadOnlyFixture(db_path)
