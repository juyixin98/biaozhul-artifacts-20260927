"""Pre-operation target snapshot.

Reads target table metadata and the full set of key columns (plus every
column referenced by conditions/assignments) in one consistent read, and
scans for duplicate target keys under the configured NULL policy.

The snapshot is the *only* target state the planner sees. Matching cannot
observe rows inserted earlier in the same batch because inserts exist solely
in the plan until commit time - this module materializes target state before
any decision is made.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from .contract import MergeSpec, TargetRow
from .errors import (
    TARGET_DUPLICATE_KEY_CODE,
    TARGET_TABLE_MISSING_CODE,
    StateConflictError,
)
from .utils import fingerprint, key_group_token, key_sort_token, quote_ident


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        cur = conn.execute(
            "SELECT name FROM pragma_table_info(?) ORDER BY cid", (table,)
        )
    except sqlite3.DatabaseError as exc:
        raise StateConflictError(
            TARGET_TABLE_MISSING_CODE,
            f"cannot read target table {table!r}: {exc}",
            {"table": table},
        )
    cols = [r[0] for r in cur.fetchall()]
    if not cols:
        raise StateConflictError(
            TARGET_TABLE_MISSING_CODE,
            f"target table {table!r} does not exist",
            {"table": table},
        )
    return cols


def snapshot_target(
    conn: sqlite3.Connection, spec: MergeSpec, needed_columns: Iterable[str]
) -> tuple[list[TargetRow], list[str], str]:
    """Read all target rows (rowid + needed columns) and fingerprint them.

    ``needed_columns`` must include key columns, update/insert target columns
    and every T.* column referenced by conditions. Rows are read as plain
    dicts; SQLite JSON columns are returned as encoded strings by default and
    stay strings.
    """
    columns = table_columns(conn, spec.target_table)
    colset = set(columns)

    missing = [c for c in needed_columns if c not in colset]
    if missing:
        from .errors import SPEC_INVALID_CODE, InputError

        raise InputError(
            SPEC_INVALID_CODE,
            "spec references target columns that do not exist",
            {"missing": sorted(missing), "target_columns": columns},
        )

    select_cols = sorted(colset & set(needed_columns) | set(spec.key_columns), key=columns.index)
    quoted = ", ".join(quote_ident(c) for c in select_cols)
    sql = f"SELECT rowid, {quoted} FROM {quote_ident(spec.target_table)}"
    try:
        cur = conn.execute(sql)
    except sqlite3.OperationalError as exc:
        raise StateConflictError(
            TARGET_TABLE_MISSING_CODE,
            f"cannot read target table {spec.target_table!r}: {exc}",
            {"table": spec.target_table},
        )

    rows: list[TargetRow] = []
    plain: list[dict[str, Any]] = []
    for record in cur.fetchall():
        rowid = record[0]
        values = {c: record[i + 1] for i, c in enumerate(select_cols)}
        rows.append(TargetRow(rowid=rowid, values=values))
        plain.append(values)

    fp = fingerprint([_fp_values(r.values, spec.key_columns) for r in rows])
    return rows, columns, fp


def _fp_values(values: dict[str, Any], _keys: tuple[str, ...]) -> dict[str, Any]:
    # Fingerprint over all read columns (read-set is fixed per run).
    return values


def find_duplicate_keys(
    rows: Iterable[TargetRow], spec: MergeSpec
) -> list[dict[str, Any]]:
    """Return duplicate key groups under the spec's NULL policy.

    * NULLS NOT DISTINCT: NULL = NULL, so ``(1, NULL)`` repeats collide.
    * NULLS DISTINCT:     any NULL component means "can never equal", so rows
                          with NULL keys never participate in duplicates
                          (exactly mirroring planner matching).

    Groups are returned sorted deterministically (by key tokens, then rowid),
    never in accidental table order.
    """
    groups: dict[Any, list[TargetRow]] = {}
    for row in rows:
        key = tuple(row.values[k] for k in spec.key_columns)
        if spec.null_policy.value == "NULLS_DISTINCT" and any(v is None for v in key):
            continue
        token = key_group_token(key)
        groups.setdefault(token, []).append(row)

    dups: list[dict[str, Any]] = []
    for token, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: (key_sort_token(_key(r, spec)), r.rowid))
        dups.append(
            {
                "key": list(token),
                "count": len(members),
                "rowids": [r.rowid for r in members],
                "_token": key_sort_token(token),
            }
        )
    dups.sort(key=lambda d: (d["_token"], d["rowids"]))
    for d in dups:
        del d["_token"]
    return dups


def _key(row: TargetRow, spec: MergeSpec) -> tuple[Any, ...]:
    return tuple(row.values[k] for k in spec.key_columns)


def assert_no_duplicate_target_keys(rows: Iterable[TargetRow], spec: MergeSpec) -> None:
    dups = find_duplicate_keys(rows, spec)
    if dups:
        shown = dups[:10]
        raise StateConflictError(
            TARGET_DUPLICATE_KEY_CODE,
            f"target contains {len(dups)} duplicate key group(s) under policy "
            f"{spec.null_policy.value}; MERGE requires a unique match target",
            {"duplicate_groups": shown, "truncated": len(dups) > 10},
        )
