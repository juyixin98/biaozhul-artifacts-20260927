"""文本规范与操作表示。

规范要点
--------
1. 文档是 Unicode **码点序列**（Python 3 ``str`` 天然按码点索引），
   位置 ``p`` 是 0 基码点偏移，合法范围 ``0 <= p <= len(text)``。
   字节长度只用于资源限额诊断，绝不参与坐标计算。
2. 操作基元（:class:`Comp`）只有两种：
   - 插入 ``("ins", p, text, client_id, client_op_id)``：把 ``text``
     插在当前待应用文档的位置 ``p``；
   - 删除 ``("del", p, length)``：从位置 ``p`` 删除 ``length`` 个码点。
   一个客户端消息可含多个基元，但同一消息内必须满足：
   位置从右到左独立（删除区间互不重叠）、位置非负、插入文本非空、
   删除长度为正，并且 ``ins/del`` 混合时同位置按 (ins, del) 次序
   （规范流序，对应“先打后删”的自然编辑）。
3. 变换在两个“组件流”之间进行。组件流即按位置升序的基元列表，
   与 OT 经典的 retain/ins/del 线性流等价，但显式位置更易审计。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .errors import ComputationFailed, InputInvalid, StateConflict

INS = "ins"
DEL = "del"


@dataclass(frozen=True, slots=True)
class Comp:
    """一个不可变操作组件。

    插入组件的每个字符都有全局身份 ``char_ids[k]``
    （形如 ``"<client_id>#<op_id>:<k>"``）。身份与提交顺序无关，
    是三操作以上场景收敛（TP2 性质）的依据：同一锚点上的并发插入
    按身份总序排列，任何接收顺序得到同一文档。
    """

    kind: str
    pos: int
    text: str = ""          # ins：插入文本；del：""
    length: int = 0         # del：删除长度；ins：0
    client_id: str = ""     # ins 来源（并发同点排序键第一级）
    client_op_id: int = 0   # 来源消息号（排序键第二级，回声识别）
    char_ids: tuple[str, ...] = ()

    # ---- 便捷构造 ----
    @staticmethod
    def ins(pos: int, text: str, client_id: str = "", client_op_id: int = 0,
            char_ids: tuple[str, ...] | None = None) -> "Comp":
        if char_ids is None and text:
            char_ids = tuple(f"{client_id}#{client_op_id}:{k}"
                             for k in range(len(text))) if client_id else tuple("")
        return Comp(INS, pos, text, 0, client_id, client_op_id,
                    tuple(char_ids) if char_ids else ())

    @staticmethod
    def del_(pos: int, length: int) -> "Comp":
        return Comp(DEL, pos, "", length)

    @property
    def is_ins(self) -> bool:
        return self.kind == INS

    @property
    def is_del(self) -> bool:
        return self.kind == DEL

    def span_end(self) -> int:
        """删除区间右端（含）；插入组件等于 pos。"""
        return self.pos + (self.length if self.is_del else 0)

    def stamp(self, client_id: str, client_op_id: int) -> "Comp":
        if self.is_ins:
            return Comp.ins(self.pos, self.text, client_id, client_op_id)
        return self

    def char_id_at(self, k: int) -> str:
        if self.char_ids and k < len(self.char_ids) and self.char_ids[k]:
            return self.char_ids[k]
        return f"{self.client_id}#{self.client_op_id}:{k}"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": INS, "pos": self.pos, "text": self.text} \
            if self.is_ins else \
            {"type": DEL, "pos": self.pos, "length": self.length}
        if self.client_id:
            d["client_id"] = self.client_id
            d["client_op_id"] = self.client_op_id
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Comp":
        t = d.get("type")
        if t not in (INS, DEL):
            raise InputInvalid(f"未知组件类型 {t!r}", reason="UNKNOWN_TYPE", details={"got": t})
        if not isinstance(d.get("pos"), int) or isinstance(d.get("pos"), bool):
            raise InputInvalid("pos 必须是整数", reason="BAD_POS")
        pos = d["pos"]
        if t == INS:
            text = d.get("text")
            if not isinstance(text, str):
                raise InputInvalid("ins.text 必须是字符串", reason="BAD_TEXT")
            cid = d.get("client_id", "") or ""
            opid = d.get("client_op_id", 0)
            if not isinstance(cid, str) or not isinstance(opid, int) or isinstance(opid, bool):
                raise InputInvalid("来源标识类型错误", reason="BAD_ORIGIN")
            return Comp.ins(pos, text, cid, opid)
        length = d.get("length")
        if not isinstance(length, int) or isinstance(length, bool):
            raise InputInvalid("del.length 必须是整数", reason="BAD_LENGTH")
        return Comp.del_(pos, length)


def comps_to_json(comps: Sequence[Comp]) -> str:
    return json.dumps([c.to_dict() for c in comps], ensure_ascii=False, sort_keys=True)


def comps_from_json(raw: str) -> list[Comp]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ComputationFailed("存储中的操作数据损坏", reason="CORRUPT_HISTORY",
                                details={"detail": str(e)}) from e
    if not isinstance(data, list):
        raise ComputationFailed("存储中的操作数据损坏：不是数组", reason="CORRUPT_HISTORY")
    return [Comp.from_dict(d) for d in data]


def normalize(ops: Iterable[Comp]) -> list[Comp]:
    """合并同位置同类型相邻组件，输出稳定规范流（按 pos 升序，同点 ins 先 del 后）。"""
    out: list[Comp] = []
    keyed = sorted(
        ops,
        key=lambda c: (c.pos, 0 if c.is_ins else 1, c.client_id, c.client_op_id),
    )
    for c in keyed:
        if c.is_ins and c.text == "":
            continue
        if c.is_del and c.length == 0:
            continue
        if out:
            p = out[-1]
            if (p.kind == c.kind and p.pos == c.pos and p.is_ins
                    and p.client_id == c.client_id and p.client_op_id == c.client_op_id):
                out[-1] = Comp.ins(p.pos, p.text + c.text, p.client_id, p.client_op_id)
                continue
        out.append(c)
    return out


def parse_message(raw_components: Sequence[dict[str, Any]]) -> list[Comp]:
    """解析并校验一个客户端提交消息，返回规范化组件流。

    校验内容：JSON 形状、pos/length 取值、空插入、同消息重叠删除、
    同点重复键冲突。注意：坐标是否落在文档内属于 *状态* 校验，
    需要基线文档，在 engine 层完成。
    """
    if not isinstance(raw_components, (list, tuple)):
        raise InputInvalid("ops 必须是组件数组", reason="BAD_OPS")
    if len(raw_components) == 0:
        raise InputInvalid("ops 不能为空消息", reason="EMPTY_OPS")
    comps = [Comp.from_dict(d) for d in raw_components]

    # 来源一致性：同一消息里所有插入必须属于同一个 (client_id, client_op_id)
    origins = {(c.client_id, c.client_op_id) for c in comps if c.is_ins}
    if len(origins) > 1:
        raise InputInvalid("一条消息中的插入必须来自同一个客户端操作",
                           reason="MIXED_ORIGIN", details={"origins": sorted(map(str, origins))})

    seen: set[tuple[int, int]] = set()
    for c in comps:
        if c.pos < 0:
            raise InputInvalid("pos 不能为负", reason="NEG_POS", details={"pos": c.pos})
        if c.is_ins:
            if c.text == "":
                raise InputInvalid("ins.text 不能为空", reason="EMPTY_INS")
        else:
            if c.length <= 0:
                raise InputInvalid("del.length 必须为正整数",
                                   reason="BAD_LENGTH", details={"length": c.length})
            k = (c.pos, c.length)
            if k in seen:
                raise InputInvalid("同一消息出现重复删除组件", reason="DUP_COMPONENT",
                                   details={"pos": c.pos, "length": c.length})
            seen.add(k)

    ordered = sorted(comps, key=lambda c: (c.pos, 0 if c.is_ins else 1))
    # 删除区间两两不重叠（允许端点相接）
    dels = [c for c in ordered if c.is_del]
    for a, b in zip(dels, dels[1:]):
        if a.pos + a.length > b.pos:
            raise InputInvalid("同一消息的删除区间互相重叠",
                               reason="OVERLAP_IN_MESSAGE",
                               details={"a": [a.pos, a.length], "b": [b.pos, b.length]})
    # 规范化（合并同点同键插入）；同点不同键插入的总序由变换层保证。
    return normalize(comps)


def apply_stream(text: str, ops: Sequence[Comp]) -> str:
    """把规范流应用到文本上。

    删除按从右到左执行，使左侧坐标不受影响；插入在所有删除之后按
    从右到左执行。任何越界都抛 :class:`StateConflict`（属于状态冲突，
    不是输入格式问题）。
    """
    chars = text
    dels = [c for c in ops if c.is_del]
    inss = [c for c in ops if c.is_ins]
    for c in sorted(dels, key=lambda c: c.pos, reverse=True):
        end = c.pos + c.length
        if c.pos < 0 or end > len(chars):
            raise StateConflict(
                "删除越界",
                reason="DELETE_OUT_OF_RANGE",
                details={"pos": c.pos, "length": c.length, "doc_len": len(chars)},
            )
        chars = chars[:c.pos] + chars[end:]
    for c in sorted(inss, key=lambda c: (c.pos, c.client_id, c.client_op_id), reverse=True):
        if c.pos < 0 or c.pos > len(chars):
            raise StateConflict(
                "插入越界",
                reason="INSERT_OUT_OF_RANGE",
                details={"pos": c.pos, "doc_len": len(chars)},
            )
        chars = chars[:c.pos] + c.text + chars[c.pos:]
    return chars


def stream_len_delta(ops: Sequence[Comp]) -> int:
    return sum(len(c.text) for c in ops if c.is_ins) - sum(c.length for c in ops if c.is_del)


def validate_against_base(base: str, ops: Sequence[Comp]) -> None:
    """用基线文档完整校验一遍操作（坐标 + 删除文本不变量）。"""
    n = len(base)
    dels = [c for c in ops if c.is_del]
    for c in ops:
        if c.is_ins:
            if not (0 <= c.pos <= n):
                raise StateConflict("插入位置超出基线文档",
                                    reason="INSERT_OUT_OF_RANGE",
                                    details={"pos": c.pos, "doc_len": n})
        else:
            if c.pos < 0 or c.pos + c.length > n:
                raise StateConflict("删除区间超出基线文档",
                                    reason="DELETE_OUT_OF_RANGE",
                                    details={"pos": c.pos, "length": c.length, "doc_len": n})
    for a, b in zip(dels, dels[1:]):
        if a.pos + a.length > b.pos:
            raise StateConflict("删除区间重叠（基线视角）", reason="OVERLAP_AGAINST_BASE")
    # 删除文本不变量在服务端由调用方提供 del_text 时另行校验（见 engine）。


def code_points(s: str) -> int:
    return len(s)


def byte_len(s: str) -> int:
    """仅用于限额诊断。"""
    return len(s.encode("utf-8"))
