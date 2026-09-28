"""计划核心：多规则同轮匹配、重叠消解、字节范围验证、计划绑定。

匹配与消解规则（本服务的确定性契约）
====================================

设规则按请求顺序声明，序号 ``d`` (0-based)，显式优先级 ``priority``。

1. **同轮扫描**：所有规则只在 **原始文本** 上各跑一遍 bump-along 扫描
   （见 :mod:`app.engine`）。命中集合在规划阶段一次性确定，替换文本不参与
   匹配，因此 **替换内容不会重新进入同轮匹配**。

2. **候选排序键**（字典序，越小越优先被考察）::

       (char_start, consuming_before_zero_width, priority, declaration_order, char_end_desc)

   其中 consuming_before_zero_width：消耗型命中=0，零宽命中=1。
   含义：
   * 更靠左的匹配先开始；
   * **同一起点，消耗型优先于零宽**（零宽前进规则不允许纯插入的零宽规则
     “挡在”真正吃掉字符的规则前面；这也是 stdlib ``re.sub`` 交替的行为）；
   * 再按声明的同轮优先级（数字小者优先）；
   * 同优先级按声明顺序（先声明者），保证稳定可重放；
   * 同起点同优先级时跨度更长者优先（最长吃掉，减少碎片）。

3. **非重叠贪心选择**：按上序依次考察候选；若其码点区间与任一已选区间
   **相交** 则淘汰（记录为 displaced）。零宽区间 [p,p) 只与同样落在 p 的
   已选零宽区间冲突，不与在 p 结束的消耗区间冲突——即允许“替换后紧跟一个
   插入”，但同一点只允许一个零宽动作（相邻零宽按排序键取一个）。

4. 每个入选命中在规划时完成：
   * 捕获模板渲染（先验证、后渲染；严格模式可选组缺失即失败）；
   * **原始字节范围验证**（码点→UTF-8 字节并往返切片校验）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import engine, template as tpl
from .config import LIMITS
from .errors import (
    InvalidRuleError,
    PayloadTooLargeError,
    ResourceExhaustedError,
)
from .textutil import ByteOffsetMap, TextSpec, make_spec


@dataclass(frozen=True)
class PreparedRule:
    declaration_order: int
    rule_id: str
    priority: int
    strict_captures: bool
    compiled: engine.CompiledRulePattern
    parsed_template: tpl.ParsedTemplate
    raw_pattern: str
    raw_template: str
    flags: str


@dataclass
class Candidate:
    rule: PreparedRule
    hit: engine.MatchHit
    replacement: str = ""
    byte_start: int = -1
    byte_end: int = -1

    # 排序键：见模块文档
    def sort_key(self) -> tuple[int, int, int, int, int]:
        return (
            self.hit.start,
            0 if self.hit.end > self.hit.start else 1,
            self.rule.priority,
            self.rule.declaration_order,
            -(self.hit.end - self.hit.start),
        )


@dataclass
class Displaced:
    rule_id: str
    char_start: int
    char_end: int
    reason: str


@dataclass
class Plan:
    source_spec: TextSpec
    char_len: int
    rules: list[PreparedRule]
    chosen: list[Candidate] = field(default_factory=list)
    displaced: list[Displaced] = field(default_factory=list)

    @property
    def zero_width_count(self) -> int:
        return sum(1 for c in self.chosen if c.hit.is_zero_width)


def prepare_rules(rule_inputs: list) -> list[PreparedRule]:
    """编译全部规则并预解析模板。任一规则失败即整批拒绝（原子性）。"""
    if len(rule_inputs) > LIMITS.max_rules_per_plan:
        raise InvalidRuleError(
            "too many rules",
            details={"count": len(rule_inputs), "limit": LIMITS.max_rules_per_plan},
        )
    seen_ids: set[str] = set()
    prepared: list[PreparedRule] = []
    for d, ri in enumerate(rule_inputs):
        if ri.rule_id in seen_ids:
            raise InvalidRuleError(
                "duplicate rule_id in same plan", details={"rule_id": ri.rule_id}
            )
        seen_ids.add(ri.rule_id)
        compiled = engine.compile_pattern(
            ri.pattern, frozenset(ri.flags), pattern_id=ri.rule_id
        )
        known_names = frozenset(n for n in compiled.group_names[1:] if n is not None)
        parsed = tpl.parse_template(ri.template, known_names, compiled.group_count)
        prepared.append(
            PreparedRule(
                declaration_order=d,
                rule_id=ri.rule_id,
                priority=ri.priority,
                strict_captures=ri.strict_captures,
                compiled=compiled,
                parsed_template=parsed,
                raw_pattern=ri.pattern,
                raw_template=ri.template,
                flags=ri.flags,
            )
        )
    return prepared


def build_plan(text: str, rule_inputs: list) -> Plan:
    """从规范文本与规则输入构建完整替换计划（不落库）。"""
    if len(text) > LIMITS.max_text_chars:
        raise PayloadTooLargeError(
            "source text exceeds char budget",
            details={"char_len": len(text), "limit": LIMITS.max_text_chars},
        )
    spec = make_spec(text)
    rules = prepare_rules(rule_inputs)

    # 1) 每规则独立 bump-along 扫描
    candidates: list[Candidate] = []
    for rule in rules:
        hits = engine.scan_nonoverlapping(rule.compiled, text)
        for hit in hits:
            candidates.append(Candidate(rule=rule, hit=hit))
        if len(candidates) > LIMITS.max_matches_per_plan:
            raise ResourceExhaustedError(
                "raw candidate count exceeded plan budget",
                details={
                    "limit": LIMITS.max_matches_per_plan,
                    "rule_id": rule.rule_id,
                },
            )

    # 2) 确定性排序
    candidates.sort(key=Candidate.sort_key)

    # 3) 重叠消解 + 4) 渲染与字节范围验证
    #
    # 扫描线，O(log n)/候选。候选按 (start, …) 非降排序。用最小堆维护所有
    # “末端仍越过当前 s”的已选消耗区间（堆顶 end 最小，惰性弹出 end<=s）。
    # 已选消耗区间彼此不相交但可能嵌套（更长区间先入选，与之相交的较短区间
    # 被淘汰）；挡住某候选的“获胜区间”是当前活跃区间里起点最早（跨度最大）的
    # 那个，记为 deepest，随选择更新、在其自身闭合时从堆中重建，与朴素地遍历
    # 所有已选区间的判定完全一致。
    offset_map = ByteOffsetMap(text)
    chosen: list[Candidate] = []
    displaced: list[Displaced] = []
    import heapq

    active: list[tuple[int, int, Candidate]] = []  # (end, start, cand)
    deepest: Candidate | None = None
    zero_owner: dict[int, Candidate] = {}

    def winner_reason(winner: Candidate, cand: Candidate) -> str:
        if winner.rule.priority < cand.rule.priority:
            return "covered_by_higher_priority"
        if winner.rule.priority > cand.rule.priority:
            # 排序键中 start/零宽位/跨度更优也可能让低优先级者先入选；
            # 这不是“声明的同轮优先级”胜利，按位置/跨度更早归因到同优先级类。
            return "covered_by_earlier_same_priority"
        # 优先级相等：先声明者（或同点跨度更长者）胜
        return "covered_by_earlier_same_priority"

    def covering(s: int) -> Candidate | None:
        nonlocal deepest
        # 弹出末端已不越过 s 的区间
        while active and active[0][0] <= s:
            heapq.heappop(active)
        if deepest is not None and deepest.hit.end <= s:
            deepest = None
        if deepest is None and active:
            # 从活跃区间重建最深者（仅在最深者刚闭合时发生，摊还很便宜）
            deepest = min((t[2] for t in active), key=lambda c: c.hit.start)
        return deepest

    for cand in candidates:
        s, e = cand.hit.start, cand.hit.end
        blocker = covering(s)

        blocked_by: Candidate | None = None
        if s == e:  # 零宽
            owner = zero_owner.get(s)
            if owner is not None:
                blocked_by = owner
            elif blocker is not None and blocker.hit.start < s:
                # 仅严格落在区间内部 cs<s<ce；两端点允许
                blocked_by = blocker
        else:  # 消耗型：与任一活跃区间相交（cs<=s<e 且 s<ce）
            if blocker is not None:
                blocked_by = blocker

        if blocked_by is not None:
            displaced.append(
                Displaced(
                    rule_id=cand.rule.rule_id,
                    char_start=s,
                    char_end=e,
                    reason=winner_reason(blocked_by, cand),
                )
            )
            continue

        # 入选：渲染（引用已在 prepare 阶段验证；此处处理可选组缺失）
        cand.replacement = tpl.render(
            cand.rule.parsed_template,
            cand.hit,
            strict_captures=cand.rule.strict_captures,
        )
        # 原始字节范围：先转换再往返验证
        b0, b1 = offset_map.verify_byte_span(s, e)
        cand.byte_start, cand.byte_end = b0, b1

        chosen.append(cand)
        if e > s:
            heapq.heappush(active, (e, s, cand))
            if deepest is None or s < deepest.hit.start or (
                s == deepest.hit.start and e > deepest.hit.end
            ):
                deepest = cand
        else:
            zero_owner[s] = cand

    # chosen 已按确定性考察顺序（同点：消耗型先于零宽、高优先级先）入列，
    # 这正是 apply 归并所需顺序；不再重排。
    return Plan(
        source_spec=spec,
        char_len=len(text),
        rules=rules,
        chosen=chosen,
        displaced=displaced,
    )
