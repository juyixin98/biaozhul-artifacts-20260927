"""建表服务：schema 校验与资源上限合并。"""
from __future__ import annotations

import json
import uuid
from typing import Any

from app.config import DEFAULT_LIMITS
from app.contracts.types import validate_schema
from app.metadata.store import Store


def create_table(
    store: Store, *, run_id: str, name: str, columns: list[dict[str, Any]],
    primary_key: list[str], config: dict[str, int] | None,
) -> dict[str, Any]:
    validate_schema(columns, primary_key)
    limits = dict(DEFAULT_LIMITS)
    if config:
        unknown = sorted(set(config) - set(DEFAULT_LIMITS))
        if unknown:
            from app.errors import ValidationError

            raise ValidationError(
                "UNKNOWN_LIMIT", "unknown resource limit key",
                {"keys": unknown, "supported": sorted(DEFAULT_LIMITS)},
            )
        for k, v in config.items():
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                from app.errors import ValidationError

                raise ValidationError("INVALID_LIMIT", f"limit {k!r} must be a positive integer")
        limits.update(config)

    table_id = f"tbl-{uuid.uuid4().hex[:16]}"
    with store.transaction() as conn:
        store.create_table(
            conn, table_id=table_id, name=name, columns=columns,
            primary_key=primary_key, config=limits,
        )
        store.insert_event(
            conn, run_id=run_id, table_id=table_id, event_type="TABLE_CREATED",
            payload={"name": name, "columns": columns, "primary_key": primary_key,
                    "config": limits},
        )
    return {"table_id": table_id, "name": name, "columns": columns,
            "primary_key": primary_key, "config": limits}
