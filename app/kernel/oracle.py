"""独立参考 oracle —— 与被测内核 scanner 完全分开的第二实现。

只用 Python 标准库，不 import pyarrow、不读 Parquet、不碰 SQLite：输入场景直接携带
规范化后的内存行，依据同一份《边界语义》独立重放全部提交并逐版本给出每行处置。
因此测试的期望值不来自被测实现本身；fixture 中的期望（fixtures 包）则是第三来源，
部分场景同时比对 fixture 手写期望，形成三方对照。

语义（与 README 一致，独立表述以防共同实现偏差）：
- 每个快照分配严格递增 seq。
- 位置删除针对“提交时仍存活”的文件当前行号；文件重写后是新文件身份，旧位置删除不落新文件。
- 等值删除（dseq, 键元组 K）删除所有 added_seq < dseq 的存活文件中键值等于 K 的行；
  删除向量中的 NULL 键元组不删除任何行；数据键 NULL 的行永不被等值删除命中。
- 行的处置：先判删除，再判过滤；被删除行不参与过滤。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---- 与 app.contracts.types 相同语义的独立规范化实现（刻意复制而非导入） ------------
def _norm(value: Any, logical_type: str) -> Any:
    if value is None:
        return None
    if logical_type == "string":
        if not isinstance(value, str):
            raise ValueError(f"expected string, got {value!r}")
        return value
    if logical_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"expected boolean, got {value!r}")
        return value
    if logical_type in ("int", "long"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"expected integer, got {value!r}")
        lo, hi = (-(2**31), 2**31 - 1) if logical_type == "int" else (-(2**63), 2**63 - 1)
        if not lo <= value <= hi:
            raise ValueError(f"integer {value} out of {logical_type}")
        return value
    if logical_type == "double":
        if isinstance(value, bool):
            raise ValueError("bool not accepted as double")
        if isinstance(value, int):
            value = float(value)
        if not isinstance(value, float):
            raise ValueError(f"expected number, got {value!r}")
        return value
    if logical_type == "date":
        import datetime as dt
        if not isinstance(value, str):
            raise ValueError("date must be YYYY-MM-DD string")
        dt.date.fromisoformat(value)
        return value
    raise ValueError(f"unknown type {logical_type!r}")


@dataclass
class _Row:
    file_ref: str
    position: int
    added_seq: int
    values: dict[str, Any]


@dataclass
class _PosDel:
    seq: int
    target_ref: str
    positions: set[int]


@dataclass
class _EqDel:
    seq: int
    key_columns: tuple[str, ...]
    keys: list[tuple[Any, ...]]  # 已保留 NULL 元组（命中规则中跳过）


@dataclass
class VersionExpectation:
    seq: int
    # 全量逐行处置（含删除/过滤），顺序按 (file_ref, position)
    dispositions: list[dict[str, Any]]
    # 仅存活且通过过滤的行（与 GET /rows 对齐）
    live_rows: list[dict[str, Any]]
    files: list[str]


@dataclass
class OracleState:
    name: str
    columns: list[dict[str, Any]]
    primary_key: list[str]
    _seq: int = 0
    # ref -> {"added_seq", "alive", "rows":[dict]}
    _files: dict[str, dict[str, Any]] = field(default_factory=dict)
    _pos_dels: list[_PosDel] = field(default_factory=list)
    _eq_dels: list[_EqDel] = field(default_factory=list)
    versions: list[VersionExpectation] = field(default_factory=list)

    @property
    def types(self) -> dict[str, str]:
        return {c["name"]: c["type"] for c in self.columns}

    # ---- 提交重放 ------------------------------------------------------
    def commit(self, operations: list[dict[str, Any]]) -> VersionExpectation:
        self._seq += 1
        seq = self._seq
        for op in operations:
            kind = op["op"]
            if kind == "append":
                self._add_file(op["ref"], op["rows"], seq)
            elif kind == "rewrite":
                for drop in op["drops"]:
                    if drop not in self._files or not self._files[drop]["alive"]:
                        raise ValueError(f"rewrite drops non-live file {drop!r}")
                    self._files[drop]["alive"] = False
                self._add_file(op["ref"], op["rows"], seq)
            elif kind == "position_delete":
                target = op["target_file"]
                if target not in self._files or not self._files[target]["alive"]:
                    raise ValueError(f"position delete target {target!r} is not a live file")
                n = len(self._files[target]["rows"])
                for p in op["positions"]:
                    if not isinstance(p, int) or isinstance(p, bool) or p < 0 or p >= n:
                        raise ValueError(f"position {p} out of range [0,{n}) in {target!r}")
                if len(set(op["positions"])) != len(op["positions"]):
                    raise ValueError("duplicate positions in one position_delete operation")
                self._pos_dels.append(_PosDel(seq, target, set(op["positions"])))
            elif kind == "equality_delete":
                self._eq_dels.append(self._build_eq_delete(op["predicates"], seq))
            else:
                raise ValueError(f"unknown op {kind!r}")
        version = self._snapshot(seq)
        self.versions.append(version)
        return version

    def _add_file(self, ref: str, raw_rows: list[dict[str, Any]], seq: int) -> None:
        if ref in self._files:
            raise ValueError(f"duplicate file ref {ref!r}")
        rows = [{k: _norm(r.get(k), t) for k, t in self.types.items()} for r in raw_rows]
        self._files[ref] = {"added_seq": seq, "alive": True, "rows": rows}

    def _build_eq_delete(self, predicates: list[dict[str, Any]], seq: int) -> _EqDel:
        if not predicates:
            raise ValueError("equality_delete requires at least one predicate")
        keys: list[tuple[Any, ...]] = []
        key_cols = tuple(self.primary_key)
        for pred in predicates:
            key = pred.get("key")
            if not isinstance(key, dict) or set(key) != set(key_cols):
                raise ValueError(f"predicate key must contain exactly {list(key_cols)}")
            keys.append(tuple(_norm(key[k], self.types[k]) for k in key_cols))
        return _EqDel(seq, key_cols, keys)

    # ---- 版本读 --------------------------------------------------------
    def _reasons(self, row: _Row) -> list[dict[str, Any]]:
        reasons: list[dict[str, Any]] = []
        for pd in self._pos_dels:
            if pd.target_ref == row.file_ref and row.position in pd.positions:
                reasons.append({"kind": "POSITION", "seq": pd.seq})
        for ed in self._eq_dels:
            if self._file_added(row.file_ref) >= ed.seq:
                continue  # 序列号窗口：后来插入的行不删
            data_key = tuple(row.values.get(k) for k in ed.key_columns)
            if any(v is None for v in data_key):
                continue  # 数据键 NULL
            for del_key in ed.keys:
                if any(v is None for v in del_key):
                    continue  # 删除向量 NULL 不命中
                if del_key == data_key:
                    reasons.append(
                        {"kind": "EQUALITY", "seq": ed.seq,
                         "key": dict(zip(ed.key_columns, del_key))}
                    )
        return reasons

    def _file_added(self, ref: str) -> int:
        return self._files[ref]["added_seq"]

    def _snapshot(self, seq: int) -> VersionExpectation:
        dispositions: list[dict[str, Any]] = []
        live_rows: list[dict[str, Any]] = []
        live_refs = sorted(r for r, f in self._files.items() if f["alive"])
        for ref in live_refs:
            f = self._files[ref]
            for pos, values in enumerate(f["rows"]):
                row = _Row(ref, pos, f["added_seq"], values)
                reasons = self._reasons(row)
                dispositions.append(
                    {
                        "file_ref": ref,
                        "position": pos,
                        "added_seq": f["added_seq"],
                        "deleted": bool(reasons),
                        "reasons": reasons,
                        "values": values,
                    }
                )
                if not reasons:
                    live_rows.append({"file_ref": ref, "position": pos, "values": values})
        return VersionExpectation(seq=seq, dispositions=dispositions, live_rows=live_rows, files=live_refs)

    # ---- 过滤（独立实现；删除先于过滤） --------------------------------
    def evaluate_filter(self, node: dict[str, Any], row: dict[str, Any]) -> bool:
        if "and" in node:
            return all(self.evaluate_filter(c, row) for c in node["and"])
        if "or" in node:
            return any(self.evaluate_filter(c, row) for c in node["or"])
        if "not" in node:
            return not self.evaluate_filter(node["not"], row)
        col, op = node["column"], node["op"]
        val = row.get(col)
        if op == "is_null":
            return val is None
        if op == "not_null":
            return val is not None
        other = node["value"]
        if val is None or other is None:
            return False
        if op == "=":
            return val == other
        if op in ("!=", "<>"):
            return val != other
        if op == "<":
            return val < other
        if op == "<=":
            return val <= other
        if op == ">":
            return val > other
        if op == ">=":
            return val >= other
        raise ValueError(f"unknown op {op!r}")

    def filtered_expectation(
        self,
        version: VersionExpectation,
        filter: dict[str, Any] | None = None,
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        """模拟 /rows 与 /explain 的过滤+投影输出（处置判定仍来自未过滤快照）。"""
        kept, deleted, filtered_out = [], [], []
        for d in version.dispositions:
            projected = d["values"] if columns is None else {k: d["values"][k] for k in columns}
            if d["deleted"]:
                deleted.append({**d, "values": projected, "disposition": "DELETED"})
                continue
            if filter is not None and not self.evaluate_filter(filter, d["values"]):
                filtered_out.append({**d, "values": projected, "disposition": "FILTERED"})
                continue
            kept.append({**d, "values": projected, "disposition": "KEPT"})
        return {"kept": kept, "deleted": deleted, "filtered": filtered_out}
