r"""独立参考实现（oracle）。

与被测核心的隔离原则
====================
* 只导入 Python 标准库（``re`` / ``hashlib``），**不导入 app.\* 任何模块**。
* 模板语法由本文件内一份独立的解析器重新实现；即使核心模板解析器有共同
  作者偏差，也不会互相印证。
* 对极简单的场景，部分测试还会直接给出“手写常量”期望（双重独立）。

因此参考答案不是由被测核心生成的。它与被测实现只共享一份规格（排序键、
零宽前进、严格捕获），两边各自实现并在测试中对账。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# stdlib re 标志名与服务标志子集的映射（oracle 端独立声明）。
# 关键规格对齐：服务使用 Google RE2，其字符类 \w \d \s 默认仅 ASCII
# （RE2 perl_classes=False）。stdlib re 默认是 Unicode 语义，因此 oracle
# 统一加 re.ASCII，保证两边对多字节文本的字符类判定一致。
_FLAG_MAP = {"i": re.IGNORECASE, "s": re.DOTALL, "m": re.MULTILINE}
_ASCII = re.ASCII


@dataclass(frozen=True)
class OGroup:
    index: int
    name: str | None
    text: str | None
    start: int
    end: int


@dataclass(frozen=True)
class OHit:
    rule_id: str
    priority: int
    decl: int
    strict_captures: bool
    start: int
    end: int
    text: str
    groups: tuple[OGroup, ...]
    template: str  # 原始模板串，渲染时用 oracle 自己的渲染器

    @property
    def zw(self) -> bool:
        return self.start == self.end


# --------------------------------------------------------------------------- #
# 单规则 bump-along（显式游标，不依赖引擎内部迭代策略）
# --------------------------------------------------------------------------- #
def o_scan(pattern: str, flags: str, text: str, rule_ctx) -> list[OHit]:
    rx = re.compile(pattern, _flags(flags))
    names = {v: k for k, v in rx.groupindex.items()}  # index -> name
    hits: list[OHit] = []
    pos = 0
    n = len(text)
    while pos <= n:
        m = rx.search(text, pos)
        if m is None:
            break
        s, e = m.start(), m.end()
        groups = []
        for i in range(1, rx.groups + 1):
            gs, ge = m.span(i)
            groups.append(
                OGroup(
                    index=i,
                    name=names.get(i),
                    text=m.group(i),
                    start=gs,
                    end=ge,
                )
            )
        hits.append(
            OHit(
                rule_id=rule_ctx["rule_id"],
                priority=rule_ctx["priority"],
                decl=rule_ctx["decl"],
                strict_captures=rule_ctx["strict_captures"],
                start=s,
                end=e,
                text=m.group(0),
                groups=tuple(groups),
                template=rule_ctx["template"],
            )
        )
        pos = e if e > s else s + 1
    return hits


def _flags(flags: str) -> int:
    val = _ASCII
    for f in flags:
        val |= _FLAG_MAP[f]
    return val


# --------------------------------------------------------------------------- #
# 多规则消解：与 app.planning 同规格的独立实现
# --------------------------------------------------------------------------- #
def o_key(h: OHit):
    return (
        h.start,
        0 if not h.zw else 1,
        h.priority,
        h.decl,
        -(h.end - h.start),
    )


@dataclass(frozen=True)
class OChoice:
    hit: OHit
    replacement: str


def o_plan(text: str, rules: list[dict]) -> tuple[list[OChoice], list[tuple]]:
    """返回 (入选动作, 被淘汰(命中, reason))。入选顺序即应用归并顺序。"""
    candidates: list[OHit] = []
    for decl, r in enumerate(rules):
        ctx = {
            "rule_id": r["rule_id"],
            "priority": r.get("priority", 100),
            "decl": decl,
            "strict_captures": r.get("strict_captures", True),
            "template": r["template"],
        }
        candidates.extend(o_scan(r["pattern"], r.get("flags", ""), text, ctx))

    candidates.sort(key=o_key)

    chosen: list[OChoice] = []
    displaced: list[tuple] = []
    consumed: list[tuple[int, int, int, int]] = []
    zero_points: dict[int, tuple[int, int]] = {}

    def reason(wp, wd, h):
        # 与 app.planning.winner_reason 同规格：priority 严格小才算更高优先级；
        # 其余（priority 相等，或因同点跨度/位置决胜）归入“同优先级更早者”。
        if wp < h.priority:
            return "covered_by_higher_priority"
        return "covered_by_earlier_same_priority"

    def blocked(h: OHit):
        s, e = h.start, h.end
        if s == e:
            if s in zero_points:
                wp, wd = zero_points[s]
                return reason(wp, wd, h)
            for cs, ce, wp, wd in consumed:
                if cs < s < ce:
                    return reason(wp, wd, h)
            return None
        for cs, ce, wp, wd in consumed:
            if s < ce and cs < e:
                return reason(wp, wd, h)
        for p, (wp, wd) in zero_points.items():
            if s <= p < e:
                return reason(wp, wd, h)
        return None

    for h in candidates:
        why = blocked(h)
        if why is not None:
            displaced.append((h, why))
            continue
        repl = o_render(h.template, h, strict=h.strict_captures)
        chosen.append(OChoice(hit=h, replacement=repl))
        if h.zw:
            zero_points[h.start] = (h.priority, h.decl)
        else:
            consumed.append((h.start, h.end, h.priority, h.decl))
    return chosen, displaced


# --------------------------------------------------------------------------- #
# 应用归并（独立实现一遍，按位置吐出原文/替换）
# --------------------------------------------------------------------------- #
def o_apply(text: str, rules: list[dict]) -> str:
    chosen, _ = o_plan(text, rules)
    out: list[str] = []
    cursor = 0
    i = 0
    while i < len(chosen):
        pos = chosen[i].hit.start
        if cursor < pos:
            out.append(text[cursor:pos])
        cursor = pos
        while i < len(chosen) and chosen[i].hit.start == pos:
            c = chosen[i]
            out.append(c.replacement)
            if not c.hit.zw:
                cursor = c.hit.end
            i += 1
    out.append(text[cursor:])
    return "".join(out)


# --------------------------------------------------------------------------- #
# 独立模板渲染器（仅支持规格内语法）
# --------------------------------------------------------------------------- #
class OTemplateError(ValueError):
    pass


class OCaptureMissing(ValueError):
    pass


def o_render(template: str, hit: OHit, *, strict: bool) -> str:
    out: list[str] = []
    i, n = 0, len(template)
    count = len(hit.groups)
    while i < n:
        c = template[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            raise OTemplateError("dangling backslash")
        nxt = template[i + 1]
        if nxt == "\\":
            out.append("\\")
            i += 2
        elif nxt == "g":
            assert template[i + 2] == "<"
            j = template.index(">", i + 3)
            ref = template[i + 3 : j]
            out.append(_ref(ref, hit, strict))
            i = j + 1
        elif nxt.isdigit():
            j = i + 1
            while j < n and template[j].isdigit():
                j += 1
            out.append(_ref(template[i + 1 : j], hit, strict))
            i = j
        else:
            raise OTemplateError(f"bad escape {nxt}")
    return "".join(out)


def _ref(ref: str, hit: OHit, strict: bool) -> str:
    if ref.isdigit():
        idx = int(ref)
        if not 1 <= idx <= len(hit.groups):
            raise OTemplateError(f"group {ref} out of range")
        g = hit.groups[idx - 1]
    else:
        for g in hit.groups:
            if g.name == ref:
                break
        else:
            raise OTemplateError(f"name {ref} not found")
    if g.text is None:
        if strict:
            raise OCaptureMissing(f"group {ref} did not participate")
        return ""
    return g.text


def o_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def o_byte_span(text: str, start: int, end: int) -> tuple[int, int]:
    b0 = len(text[:start].encode("utf-8"))
    seg = text[start:end].encode("utf-8")
    return b0, b0 + len(seg)
