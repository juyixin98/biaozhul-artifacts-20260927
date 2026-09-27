"""独立配置加载：config/searchdsl.yaml -> 强类型 Settings。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .budget import Budget
from .schema import Schema


@dataclass(frozen=True)
class Settings:
    schema: Schema
    budget: Budget
    database_path: Path
    log_path: Path
    default_result_limit: int = 100


def load_settings(path: Optional[str | Path] = None,
                  *,
                  database_path_override: Optional[Path] = None,
                  log_path_override: Optional[Path] = None) -> Settings:
    cfg_path = Path(path) if path else Path(__file__).resolve().parents[1] / "config" / "searchdsl.yaml"
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    base = cfg_path.parent.parent
    db_override = database_path_override or (base / raw["database_path"])
    log_override = log_path_override or (base / raw["log_path"])

    field_types: Dict[str, str] = {}
    for name, spec in raw["fields"].items():
        field_types[name] = spec["type"] if isinstance(spec, dict) else str(spec)

    default_fields: List[str] = list(raw.get("default_fields", []))
    if not default_fields:
        default_fields = [f for f, t in field_types.items() if t == "text"]
    for f in default_fields:
        if field_types.get(f) != "text":
            raise ValueError(f"默认字段 {f} 必须是 text 类型")

    b = raw["budget"]
    return Settings(
        schema=Schema(field_types=field_types, default_fields=default_fields),
        budget=Budget(
            max_depth=int(b["max_depth"]),
            max_clauses=int(b["max_clauses"]),
            max_phrase_terms=int(b["max_phrase_terms"]),
            max_term_length=int(b["max_term_length"]),
        ),
        database_path=Path(db_override),
        log_path=Path(log_override),
        default_result_limit=int(raw.get("default_result_limit", 100)),
    )
