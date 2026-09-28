"""独立参考预言机（oracle）。

这份实现是测试侧的"第二真相源"，刻意不 import 任何 deleter.* 代码，
规则用另一种写法独立表达：

* 自己维护文件/版本/序列号的顺序模型；
* 自己实现 SQL 三值逻辑的等值规则；
* 自己做重写（幸存行身份延续、旧行号失效）。

测试把同一条操作流分别喂给服务和本预言机，再对逐行结论做等价映射比对。
原因码字符串故意与被测内核不同（前缀 R_），等价关系由测试显式声明，
避免"常量复用导致两边一起错"。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

# 预言机自己的判定码（与内核常量无引用关系）
R_DEL_POS = "R_position_hit"
R_DEL_EQ = "R_equality_hit"
R_KEEP_LATE = "R_late_insert"
R_KEEP_NONE = "R_no_predicate_match"
R_KEEP_NULL_PRED = "R_null_predicate_blocks"
R_KEEP_NULL_ROW = "R_null_row_key_blocks"

R_OP_APPLIED = "R_op_applied"
R_OP_ZERO = "R_op_zero"
R_OP_STALE = "R_op_stale"
R_OP_STALE_REMOVED = "R_op_stale_row_removed"
R_OP_STALE_OOR = "R_op_stale_out_of_range"

# 内核原因码 <-> 预言机原因码（测试中唯一允许的对照关系，手写维护）
KERNEL_EQUIVALENCE = {
    R_DEL_POS: "position_delete",
    R_DEL_EQ: "equality_delete",
    R_KEEP_LATE: "in_scope_insert",
    R_KEEP_NONE: "no_match",
    R_KEEP_NULL_PRED: "null_key_blocked",
    R_KEEP_NULL_ROW: "null_row_key_blocked",
    R_OP_APPLIED: "applied",
    R_OP_ZERO: "applied_zero_rows",
    R_OP_STALE: "stale_file_rewritten",
    R_OP_STALE_REMOVED: "stale_row_already_removed",
    R_OP_STALE_OOR: "stale_out_of_range",
}


@dataclass
class _Line:
    values: dict[str, Any]
    insert_seq: int
    origins: list[tuple[str, int, int]] = field(default_factory=list)
    # origins：该行作为"幸存内容"承载的所有历史父坐标（含原始载入坐标）


@dataclass
class _File:
    fid: str
    version: int
    lines: list[_Line]
    live: bool = True


@dataclass
class _PosOp:
    op_id: str
    seq: int
    fid: str
    rn: int
    bound_version: int
    bound_n: int
    hits: list[tuple[str, int]] = field(default_factory=list)
    status: str = R_OP_ZERO


@dataclass
class _EqOp:
    op_id: str
    seq: int
    wanted: tuple[Any, ...]
    hits: list[tuple[str, int]] = field(default_factory=list)
    status: str = R_OP_ZERO


class ReferenceTable:
    """与被测服务同构但完全独立的顺序模型。"""

    def __init__(self, key_columns: list[str]) -> None:
        self.key_columns = list(key_columns)
        self._clock = 0
        self.files: dict[str, _File] = {}
        self.pos_ops: list[_PosOp] = []
        self.eq_ops: list[_EqOp] = []
        # 曾被任意一次重写产物携带过的历史父坐标（含后来再次被重写的中间产物）
        self.ever_carried: set[tuple[str, int, int]] = set()

    def _tick(self) -> int:
        self._clock += 1
        return self._clock

    # ---- 事件（与服务端操作一一对应） ----
    def load(self, fid: str, rows: list[dict[str, Any]]) -> int:
        assert fid not in self.files, "file_id 重复应由驱动/服务拒绝"
        seq = self._tick()
        self.files[fid] = _File(
            fid=fid, version=1,
            lines=[_Line(copy.deepcopy(r), seq, origins=[]) for r in rows],
        )
        return seq

    def position_delete(self, op_id: str, fid: str, rn: int) -> int:
        f = self.files[fid]
        # 与服务端相同的前置校验：文件必须当前 live，行号必须在当前行数内
        if not f.live or rn >= len(f.lines):
            raise ValueError("rejected_position_delete")
        seq = self._tick()
        self.pos_ops.append(_PosOp(op_id, seq, fid, rn, f.version, len(f.lines)))
        return seq

    def equality_delete(self, op_id: str, key_values: list[Any]) -> int:
        seq = self._tick()
        self.eq_ops.append(_EqOp(op_id, seq, tuple(key_values)))
        return seq

    def rewrite(self, parent_ids: list[str], new_fid: str) -> None:
        # 独立计算当前幸存行（行身份 = 值 + insert_seq，原样延续）
        kept_lines: list[_Line] = []
        for pid in parent_ids:
            f = self.files[pid]
            assert f.live
            for rn, line in enumerate(f.lines):
                if self._is_alive(line, pid, rn)[0]:
                    nl = copy.deepcopy(line)
                    # 记录该幸存行承载的父坐标（含已有历史坐标）
                    nl.origins = line.origins + [(pid, f.version, rn)]
                    kept_lines.append(nl)
        assert kept_lines, "空重写应由服务拒绝"
        self._clock  # 重写不占序列号
        for nl in kept_lines:
            self.ever_carried.update(nl.origins)
        new_file = _File(new_fid, 1, kept_lines, live=True)
        self.files[new_fid] = new_file
        for pid in parent_ids:
            self.files[pid].live = False
        # 与内核相同的事实规则：坐标曾出现在任意重写产物血缘中 -> rewritten；
        # 否则该行在文件退出 live 的重写之前就已被删除 -> row_removed。
        for op in self.pos_ops:
            coord = (op.fid, op.bound_version, op.rn)
            if op.fid in parent_ids and self.files[op.fid].version == op.bound_version:
                if op.status in (R_OP_ZERO, R_OP_APPLIED):
                    if op.rn >= op.bound_n:
                        op.status = R_OP_STALE_OOR
                    elif coord in self.ever_carried:
                        op.status = R_OP_STALE
                    else:
                        op.status = R_OP_STALE_REMOVED

    # ---- 比较规则（独立表达：三值逻辑） ----
    @staticmethod
    def _strict_equal(x: Any, y: Any) -> bool:
        # NULL 不交给这里
        if isinstance(x, bool) or isinstance(y, bool):
            return type(x) is type(y) and x == y
        if isinstance(x, (int, float)) and not isinstance(x, bool) and \
           isinstance(y, (int, float)) and not isinstance(y, bool):
            return x == y
        if type(x) is not type(y):
            return False
        return x == y

    def _equality_hit(self, line: _Line, wanted: tuple[Any, ...]) -> tuple[bool, str | None]:
        """返回 (是否命中删除, 未命中原因码或None)。"""
        row_keys = [line.values[c] for c in self.key_columns]
        # 规则一：谓词含 NULL -> UNKNOWN，任何行都不删
        if any(w is None for w in wanted):
            return False, R_KEEP_NULL_PRED
        # 规则二：行键含 NULL -> 不与任何非 NULL 谓词相等
        if any(k is None for k in row_keys):
            return False, R_KEEP_NULL_ROW
        for x, y in zip(row_keys, wanted):
            if not self._strict_equal(x, y):
                return False, R_KEEP_NONE
        return True, None

    def _is_alive(self, line: _Line, fid: str, rn: int) -> tuple[bool, str, str | None, int | None]:
        """返回 (是否保留, 原因码, 命中op_id, 命中seq)。"""
        f = self.files[fid]
        # 位置删除：要求文件仍 live 且绑定版本==当前版本
        for op in sorted(self.pos_ops, key=lambda o: o.seq):
            if op.fid == fid and op.bound_version == f.version and op.rn == rn and f.live:
                return False, R_DEL_POS, op.op_id, op.seq
        # 等值删除：找最早的"在可见范围内"命中
        late_evidence: _EqOp | None = None
        for op in sorted(self.eq_ops, key=lambda o: o.seq):
            hit, why = self._equality_hit(line, op.wanted)
            if not hit:
                continue
            if line.insert_seq <= op.seq:
                return False, R_DEL_EQ, op.op_id, op.seq
            if late_evidence is None:
                late_evidence = op
        if late_evidence is not None:
            # 保留，但记录它"躲过"的最早越界删除（内核也给同样的证据）
            return True, R_KEEP_LATE, late_evidence.op_id, late_evidence.seq
        if not self.eq_ops:
            # 没有任何等值谓词：无"匹配"可言（NULL 阻挡的前提是存在谓词）
            return True, R_KEEP_NONE, None, None
        # 未命中的根本原因：行键 NULL 优先（NULL 行无论谓词是什么都不匹配），
        # 其次是存在谓词 NULL（否则非 NULL 行不会被 NULL 谓词挡住）
        if any(line.values[c] is None for c in self.key_columns):
            return True, R_KEEP_NULL_ROW, None, None
        if all(any(w is None for w in o.wanted) for o in self.eq_ops):
            return True, R_KEEP_NULL_PRED, None, None
        return True, R_KEEP_NONE, None, None

    # ---- 全量扫描 ----
    STALE_STATUSES = (R_OP_STALE, R_OP_STALE_REMOVED, R_OP_STALE_OOR)

    def scan_verdicts(self) -> list[dict[str, Any]]:
        verdicts: list[dict[str, Any]] = []
        # 重置每轮命中记录（stale 分类是持久事实，不能被重置）
        for op in self.pos_ops:
            op.hits = []
            if op.status not in self.STALE_STATUSES:
                op.status = R_OP_ZERO
        for op in self.eq_ops:
            op.hits = []
            op.status = R_OP_ZERO

        for fid in sorted(f for f in self.files if self.files[f].live):
            f = self.files[fid]
            for rn, line in enumerate(f.lines):
                alive, reason, op_id, op_seq = self._is_alive(line, fid, rn)
                verdicts.append({
                    "file_id": fid, "row_number": rn,
                    "insert_seq": line.insert_seq,
                    "action": "keep" if alive else "delete",
                    "oracle_reason": reason,
                    "by_delete_id": op_id, "by_seq": op_seq,
                    "values": copy.deepcopy(line.values),
                })
                if not alive:
                    if reason == R_DEL_POS:
                        next(o for o in self.pos_ops if o.op_id == op_id).hits.append((fid, rn))
                    else:
                        next(o for o in self.eq_ops if o.op_id == op_id).hits.append((fid, rn))

        for op in self.pos_ops:
            if op.status not in self.STALE_STATUSES:
                op.status = R_OP_APPLIED if op.hits else R_OP_ZERO
        for op in self.eq_ops:
            # NULL 谓词在结构上恒为 0 命中；其余按实际命中
            op.status = R_OP_APPLIED if op.hits else R_OP_ZERO
        return verdicts

    def op_statuses(self) -> dict[str, str]:
        out = {}
        for op in self.pos_ops:
            out[op.op_id] = op.status
        for op in self.eq_ops:
            out[op.op_id] = op.status
        return out
