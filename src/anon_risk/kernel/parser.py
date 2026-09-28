"""按规则解析输入：显式声明列角色、规范化缺失值、产出解析证据。

关键行为（对应需求第二阶段）：

- 准标识符列与敏感列必须**显式声明**；缺一列都报具体错误码。
- 缺失值（``None`` / 空串 / 纯空白）规范化为 :data:`NULL`，**行不会被
  删除**，每列 NULL 行数在 ``null_counts`` 证据中可见。
- 解析只做结构与值域校验；层级“包含关系”等语义校验在
  :mod:`hierarchy` 完成。
"""

from __future__ import annotations

from typing import Any, Optional

from ..errors import ErrorCode, RiskError
from ..logging_setup import get_logger
from .types import Dataset, HierarchySpec, LevelSpec, NULL

log = get_logger("parser")

_VALID_RULES = {"map", "prefix", "range"}


def _norm_cell(value: Any) -> Optional[str]:
    if value is None:
        return NULL
    if not isinstance(value, str):
        raise RiskError(
            f"单元格值必须是字符串或 null，收到 {type(value).__name__}",
            code=ErrorCode.INVALID_PARAMETER,
        )
    if value.strip() == "":
        return NULL
    return value


def _build_level(raw: dict[str, Any], column: str, index: int) -> LevelSpec:
    if not isinstance(raw, dict):
        raise RiskError(
            f"列 {column} 的第 {index + 1} 级不是对象",
            code=ErrorCode.HIERARCHY_BAD_LEVEL,
        )
    rule = raw.get("rule")
    if rule not in _VALID_RULES:
        raise RiskError(
            f"列 {column} 第 {index + 1} 级的 rule 必须是 {sorted(_VALID_RULES)}",
            code=ErrorCode.HIERARCHY_BAD_LEVEL,
            details={"column": column, "level_index": index},
        )

    name = raw.get("name")
    if name is not None and not isinstance(name, str):
        raise RiskError("层级 name 必须是字符串",
                        code=ErrorCode.HIERARCHY_BAD_LEVEL,
                        details={"column": column, "level_index": index})

    spec = LevelSpec(rule=rule, name=name)

    if rule == "map":
        mapping = raw.get("mapping")
        if not isinstance(mapping, dict) or not mapping:
            raise RiskError(
                f"列 {column} 第 {index + 1} 级的 map 规则需要非空 mapping",
                code=ErrorCode.HIERARCHY_BAD_LEVEL,
                details={"column": column, "level_index": index},
            )
        clean: dict[str, str] = {}
        for k, v in mapping.items():
            if not isinstance(k, str) or not isinstance(v, str) or v.strip() == "":
                raise RiskError(
                    f"列 {column} 第 {index + 1} 级 mapping 的键值都必须是非空字符串",
                    code=ErrorCode.HIERARCHY_BAD_LEVEL,
                    details={"column": column, "level_index": index},
                )
            clean[k] = v
        if len(clean) != len(mapping):
            raise RiskError(
                f"列 {column} 第 {index + 1} 级 mapping 含重复键",
                code=ErrorCode.HIERARCHY_DUPLICATE_LABEL,
                details={"column": column, "level_index": index},
            )
        spec = LevelSpec(rule=rule, mapping=clean, name=name)

    elif rule == "prefix":
        keep = raw.get("keep")
        if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
            raise RiskError(
                f"列 {column} 第 {index + 1} 级 prefix 规则需要正整数 keep",
                code=ErrorCode.HIERARCHY_BAD_LEVEL,
                details={"column": column, "level_index": index},
            )
        spec = LevelSpec(rule=rule, keep=keep, name=name)

    else:  # range
        bins = raw.get("bins")
        labels = raw.get("labels")
        if (
            not isinstance(bins, list)
            or len(bins) < 2
            or not all(isinstance(b, (int, float)) and not isinstance(b, bool)
                       for b in bins)
            or any(bins[i] >= bins[i + 1] for i in range(len(bins) - 1))
        ):
            raise RiskError(
                f"列 {column} 第 {index + 1} 级 range 规则需要严格递增的 bins（>=2 个端点）",
                code=ErrorCode.HIERARCHY_BAD_LEVEL,
                details={"column": column, "level_index": index},
            )
        if labels is not None:
            if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
                raise RiskError(
                    f"列 {column} 第 {index + 1} 级 labels 必须是字符串列表",
                    code=ErrorCode.HIERARCHY_BAD_LEVEL,
                    details={"column": column, "level_index": index},
                )
            if len(labels) != len(bins) - 1:
                raise RiskError(
                    f"列 {column} 第 {index + 1} 级 labels 数量必须等于箱数 {len(bins) - 1}",
                    code=ErrorCode.HIERARCHY_BAD_LEVEL,
                    details={"column": column, "level_index": index,
                             "expected": len(bins) - 1, "got": len(labels)},
                )
        spec = LevelSpec(rule=rule, bins=[float(b) for b in bins],
                         labels=labels, name=name)

    return spec


def parse_dataset(payload: dict[str, Any]) -> Dataset:
    """把 JSON 风格的请求体解析为 :class:`Dataset`，全过程不丢行。"""
    if not isinstance(payload, dict) or not payload:
        raise RiskError("请求体为空或不是对象", code=ErrorCode.EMPTY_PAYLOAD)

    columns = payload.get("columns")
    if not isinstance(columns, list) or not columns:
        raise RiskError("columns 必须是非空列表", code=ErrorCode.EMPTY_COLUMNS)
    if not all(isinstance(c, str) and c.strip() for c in columns):
        raise RiskError("columns 必须全部是非空字符串",
                        code=ErrorCode.INVALID_PARAMETER)
    if len(set(columns)) != len(columns):
        raise RiskError("columns 含重复列名",
                        code=ErrorCode.DUPLICATE_COLUMN_ROLE)

    qi = payload.get("quasi_identifiers")
    sensitive = payload.get("sensitive")
    if not isinstance(qi, list) or not qi:
        raise RiskError("必须显式声明至少一个准标识符列 quasi_identifiers",
                        code=ErrorCode.EMPTY_QUASI_IDENTIFIERS)
    if not isinstance(sensitive, list) or not sensitive:
        raise RiskError("必须显式声明至少一个敏感列 sensitive",
                        code=ErrorCode.EMPTY_SENSITIVE)

    declared = list(qi) + list(sensitive)
    if len(set(declared)) != len(declared):
        overlap = sorted({c for c in declared if declared.count(c) > 1})
        raise RiskError(
            "同一列不能同时/重复声明为准标识符和敏感列",
            code=ErrorCode.DUPLICATE_COLUMN_ROLE,
            details={"columns": overlap},
        )
    for c in declared:
        if not isinstance(c, str) or c not in columns:
            raise RiskError(
                f"声明列 {c!r} 不在 columns 中",
                code=ErrorCode.COLUMN_NOT_FOUND,
                details={"column": c if isinstance(c, str) else None},
            )

    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise RiskError("rows 必须是非空列表", code=ErrorCode.EMPTY_DATA)

    rows: list[dict[str, Optional[str]]] = []
    null_counts = {c: 0 for c in columns}
    for idx, row in enumerate(raw_rows):
        if not isinstance(row, list):
            raise RiskError(
                f"第 {idx} 行必须是列表（按 columns 顺序）",
                code=ErrorCode.ROW_WIDTH_MISMATCH,
                details={"row_index": idx, "expected_width": len(columns)},
            )
        if len(row) != len(columns):
            raise RiskError(
                f"第 {idx} 行宽度 {len(row)} 与列数 {len(columns)} 不一致",
                code=ErrorCode.ROW_WIDTH_MISMATCH,
                details={"row_index": idx, "expected_width": len(columns),
                         "actual_width": len(row)},
            )
        norm = {columns[i]: _norm_cell(v) for i, v in enumerate(row)}
        rows.append(norm)
        for c in columns:
            if norm[c] is NULL:
                null_counts[c] += 1

    # 层级（只允许挂在准标识符列上）
    hierarchies: dict[str, HierarchySpec] = {}
    raw_hier = payload.get("hierarchies") or {}
    if not isinstance(raw_hier, dict):
        raise RiskError("hierarchies 必须是以列名为键的对象",
                        code=ErrorCode.HIERARCHY_BAD_LEVEL)
    for column in qi:
        entry = raw_hier.get(column)
        if entry is None:
            raise RiskError(
                f"准标识符列 {column} 缺少泛化层级声明",
                code=ErrorCode.HIERARCHY_MISSING,
                details={"column": column},
            )
        levels_raw = entry.get("levels") if isinstance(entry, dict) else None
        if not isinstance(levels_raw, list):
            raise RiskError(
                f"列 {column} 的层级需要 levels 列表（可为空数组=无泛化能力）",
                code=ErrorCode.HIERARCHY_BAD_LEVEL,
                details={"column": column},
            )
        levels = [_build_level(lv, column, i) for i, lv in enumerate(levels_raw)]
        hierarchies[column] = HierarchySpec(column=column, levels=levels)

    dataset = Dataset(
        columns=list(columns),
        qi_columns=list(qi),
        sensitive_columns=list(sensitive),
        rows=rows,
        hierarchies=hierarchies,
        null_counts=null_counts,
    )
    log.info(
        "解析完成",
        extra={"event": {
            "rows": len(rows),
            "qi": qi,
            "sensitive": sensitive,
            "null_counts": null_counts,
        }},
    )
    return dataset
