"""脱敏安全内核。

核心保证：
1. 跨 chunk：模式匹配的尾部在被证明"不可能再延长"之前整体扣住不放行，
   通过每规则 ``prefix_hint``（锚定 ``$``）在保留尾部上搜索可能前缀，
   配合字段词法器的 ``safe_pos``，保证未完整识别的片段绝不提前输出；
2. 优先级与重叠明确：候选按（起点升序、priority 升序、长度降序、
   来源字段优先、rule_id 升序）裁决，被压制的候选逐条留痕；
3. 替换后做残留自检：所有原候选原文不得在输出中出现，所有模式在输出上
   零命中；发现残留即判内部错误，不静默；
4. 长度变化完整记录双向位置（原文偏移 ↔ 输出偏移）。

整段处理与流式处理走同一条代码路径（整段 = 单 chunk 喂入后 finalize）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .. import __version__ as ENGINE_VERSION
from ..rules.models import FieldRule, PatternRule, Profile
from .json_lex import LexEvent, StreamingJsonLexer

# --------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class Candidate:
    rule_id: str
    start: int
    end: int
    replacement: str
    priority: int
    source: str  # "pattern" | "field"
    key: str | None = None
    matched_text: str = ""


@dataclass(frozen=True)
class Rejection:
    winner: Candidate
    loser: Candidate
    reason: str  # OVERLAP_PRIORITY | OVERLAP_CONTAINED


@dataclass(frozen=True)
class MappingRecord:
    rule_id: str
    source: str
    key: str | None
    original_start: int
    original_end: int
    output_start: int
    output_end: int
    replacement: str
    original_sha256: str
    original_text: str = ""


@dataclass(frozen=True)
class Uncertainty:
    code: str
    start: int
    end: int
    detail: str


@dataclass(frozen=True)
class EmittedChunk:
    index: int
    text: str
    original_start: int
    original_end: int


@dataclass
class ProgressEvent:
    kind: str
    payload: dict[str, Any]


@dataclass
class RedactionResult:
    status: str  # "ok" | "error"
    output: str
    mappings: list[MappingRecord] = field(default_factory=list)
    uncertainties: list[Uncertainty] = field(default_factory=list)
    events: list[ProgressEvent] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    residual_findings: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    profile_name: str = ""
    profile_version: str = ""
    engine_version: str = ENGINE_VERSION
    original_length: int = 0
    output_length: int = 0


EventSink = Callable[[ProgressEvent], None]


# --------------------------------------------------------------------- #
# 重叠裁决
# --------------------------------------------------------------------- #
def resolve_overlaps(candidates: list[Candidate]) -> tuple[list[Candidate],
                                                           list[Rejection]]:
    """按确定性顺序选择互不重叠候选。

    顺序：priority 小者优先 → 起点早 → 长度长 → 字段来源 → rule_id。
    相邻（端点相接）不算重叠，各自保留。
    """
    ordered = sorted(
        candidates,
        key=lambda c: (c.priority, c.start, -c.end,
                       0 if c.source == "field" else 1, c.rule_id),
    )
    accepted: list[Candidate] = []
    rejected: list[Rejection] = []
    for cand in ordered:
        conflict = next(
            (a for a in accepted
             if cand.start < a.end and a.start < cand.end),
            None,
        )
        if conflict is None:
            accepted.append(cand)
        else:
            rejected.append(Rejection(
                winner=conflict, loser=cand,
                reason=("OVERLAP_PRIORITY"
                        if cand.priority != conflict.priority
                        else "OVERLAP_CONTAINED")))
    accepted.sort(key=lambda c: (c.start, c.end))
    return accepted, rejected


# --------------------------------------------------------------------- #
# 内核
# --------------------------------------------------------------------- #
class StreamingRedactor:
    """流式脱敏器；一个实例只服务一个请求，状态天然请求隔离。"""

    def __init__(self, profile: Profile, sink: EventSink | None = None) -> None:
        self.profile = profile
        self._sink = sink
        self._lex = StreamingJsonLexer(
            sensitive_keys=(profile.field_rule.keys_lower
                            if profile.field_rule else frozenset()),
            max_value_length=(profile.field_rule.max_value_length
                              if profile.field_rule else 0),
        )
        # 缓冲：只保留尚未发出的原文尾部
        self._buf = ""
        self._buf_off = 0          # buf[0] 在全量输入中的绝对位置
        # 边界回看：buf 之前紧邻的已发出字符，用于在缓冲起点正确求值
        # \b 与定长后顾断言（冲刷不能丢掉秘密的前导边界字符）
        self._ctx = ""
        self._ctx_off = 0
        self._total_in = 0        # 已接收原文长度
        self._chunk_index = 0
        # 词法器产出的字段候选（完成态，按 start 去重）
        self._field_events: dict[int, LexEvent] = {}
        # 已进入某次裁决的字段事件起点（随缓冲冲刷而清理）
        self._settled_field_starts: set[int] = set()
        # 已完成裁决的映射（随流式累积）
        self._mappings: list[MappingRecord] = []
        self._rejections: list[Rejection] = []
        self._uncertainties: list[Uncertainty] = []
        self._events: list[ProgressEvent] = []
        self._emitted_parts: list[str] = []
        self._out_len = 0
        self._closed = False
        self._hold = max((r.max_length for r in profile.pattern_rules),
                         default=1)
        # \b 在非字母数字↔字母数字之间；后顾断言最多回看 1 字符，
        # 故 1 字符回看足以在缓冲起点重建边界。
        self._ctx_len = max(
            (self._rule_lookbehind(r) for r in profile.pattern_rules),
            default=1)
        # 字段值的扣留由词法器 safe_pos 精确负责，这里不再叠加其长度。

    def _scan_text(self) -> str:
        """带边界回看的扫描文本（ctx + buf）。"""
        return self._ctx + self._buf

    @staticmethod
    def _rule_lookbehind(rule) -> int:
        """估计规则所需的最大回看字符数（\b 与简单后顾各计 1）。"""
        src = rule.pattern_source
        need = 1 if r"\b" in src else 0
        if "(?<!" in src or "(?<=" in src:
            need = max(need, 1)
        return need

    # ------------------------------------------------------------------ #
    @property
    def held_chars(self) -> int:
        return len(self._buf)

    @property
    def total_received(self) -> int:
        """已接收原文总字符数（流式累计）。"""
        return self._total_in

    def _emit_event(self, kind: str, payload: dict[str, Any]) -> None:
        evt = ProgressEvent(kind, payload)
        self._events.append(evt)
        if self._sink:
            self._sink(evt)

    def feed(self, chunk: str) -> EmittedChunk:
        """喂入一个输入块，返回当前可安全发出的脱敏文本（可能为空）。"""
        if self._closed:
            raise RuntimeError("redactor 已 finalize，不能再 feed")
        idx = self._chunk_index
        self._chunk_index += 1
        self._emit_event("CHUNK_RECEIVED",
                         {"index": idx, "length": len(chunk)})

        lex_result = self._lex.feed(chunk, self._total_in)
        for ev in lex_result.events:
            self._absorb_lex_event(ev)
        self._buf += chunk
        self._total_in += len(chunk)

        # 安全边界：先扣住最后 hold 个字符；再由词法器（未闭合的键/敏感
        # 值起点）、prefix_hint 收紧；最后防止边界切过已完整到达的匹配。
        boundary = self._total_in - self._hold
        boundary = max(boundary, self._buf_off)
        boundary = min(boundary, self._lex.safe_pos, self._lex.held_from)
        boundary = self._tighten_by_hints(boundary)
        boundary = self._settle_closed_fields(boundary)
        boundary = self._pull_back_across_matches(boundary)

        emitted = self._settle_prefix(boundary)
        self._emit_event("CHUNK_EMITTED", {
            "index": idx,
            "original_start": emitted.original_start,
            "original_end": emitted.original_end,
            "output_length": len(emitted.text),
            "boundary": boundary,
            "held_after": len(self._buf),
        })
        return emitted

    def finalize(self,
                 on_ambiguous_tail: str | None = None) -> RedactionResult:
        """流结束：冲刷全部尾部，做尾部歧义判定与残留自检。"""
        if self._closed:
            raise RuntimeError("redactor 已 finalize")
        self._closed = True
        tail_mode = on_ambiguous_tail or self.profile.on_ambiguous_tail

        for ev in self._lex.finalize(self._total_in):
            self._absorb_lex_event(ev)

        # 候选只接受起点在实际保留缓冲内的；回看 ctx 仅用于在缓冲起点
        # 正确求值 \b / 后顾断言。
        accepted, rejected = self._scan_and_resolve(
            self._buf_off, self._total_in)
        ambiguous = self._detect_ambiguous_tail(accepted)
        # finalize 阶段的拒绝全部留痕（中间态拒绝也会在 settle 时记录）
        for rj in rejected:
            if rj not in self._rejections:
                self._rejections.append(rj)
                self._emit_event("OVERLAP_REJECTED", {
                    "winner": rj.winner.rule_id,
                    "loser": rj.loser.rule_id,
                    "winner_span": [rj.winner.start, rj.winner.end],
                    "loser_span": [rj.loser.start, rj.loser.end],
                    "reason": rj.reason,
                })

        # 候选已在 ctx+buf 窗口上正确求值（绝对坐标，起点 >= buf_off）。
        # 输出只拼接仍保留的 buf；映射全局输出坐标基准为已发长度 _out_len。
        final_text = self._apply_candidates(
            self._buf, self._buf_off, accepted, flush=True,
            output_base=self._out_len)
        self._out_len += len(final_text)
        self._ctx, self._buf = "", ""
        output = "".join(self._emitted_parts) + final_text

        uncertainties = list(self._uncertainties)
        for amb in ambiguous:
            uncertainties.append(Uncertainty(
                code="AMBIGUOUS_TAIL",
                start=amb[0], end=amb[1],
                detail="尾部存在无法证明完整的敏感前缀（未匹配为完整秘密），"
                f"规则={amb[2]}",
            ))
            self._emit_event("UNCERTAINTY", {
                "code": "AMBIGUOUS_TAIL", "start": amb[0], "end": amb[1],
                "rule_id": amb[2]})

        residual = self._verify_no_residuals(output, accepted)
        for finding in residual:
            self._emit_event("RESIDUAL_FOUND", {"finding": finding})

        self._emit_event("FINALIZED", {
            "original_length": self._total_in,
            "output_length": len(output),
            "mappings": len(self._mappings),
            "uncertainties": len(uncertainties),
            "ambiguous_tail_mode": tail_mode,
        })

        result = RedactionResult(
            status="ok",
            output=output,
            mappings=sorted(self._mappings,
                            key=lambda m: (m.original_start, m.original_end)),
            uncertainties=uncertainties,
            events=list(self._events),
            rejected=list(self._rejections),
            residual_findings=residual,
            profile_name=self.profile.name,
            profile_version=self.profile.version,
            original_length=self._total_in,
            output_length=len(output),
        )
        if residual:
            result.status = "error"
            result.error_code = "RESIDUAL_SECRET_DETECTED"
            result.error_message = "脱敏后输出中仍检出原秘密片段，已拒绝交付"
        elif ambiguous and tail_mode == "error":
            result.status = "error"
            result.error_code = "AMBIGUOUS_TAIL"
            result.error_message = (
                f"尾部 {len(ambiguous)} 处疑似不完整敏感前缀；"
                "严格档要求调用方确认/补齐后重试")
        return result

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _absorb_lex_event(self, ev: LexEvent) -> None:
        if ev.kind == "field_value":
            # 同一区间只保留一条（finalize 可能补产生闭合事件）
            self._field_events.setdefault(ev.start, ev)
        elif ev.kind == "uncertainty":
            u = Uncertainty(ev.code or "LEX_UNCERTAINTY",
                            ev.start, ev.end, ev.detail)
            self._uncertainties.append(u)
            self._emit_event("UNCERTAINTY", {
                "code": u.code, "start": u.start, "end": u.end,
                "detail": u.detail})

    def _rule_window(self, rule) -> tuple[str, int]:
        """返回规则的扫描窗口（含回看）及其绝对起点。"""
        lb = self._rule_lookbehind(rule)
        start = max(self._buf_off - lb, self._ctx_off, 0)
        text = self._scan_text()
        return text[start - self._ctx_off:], start

    def _tighten_by_hints(self, boundary: int) -> int:
        """在保留尾部搜索各规则的可能前缀，收紧可放行边界。"""
        if not self._buf:
            return min(boundary, self._buf_off)
        for rule in self.profile.pattern_rules:
            window_start = max(self._buf_off,
                               self._total_in - rule.max_length)
            window = self._buf[window_start - self._buf_off:]
            m = rule.prefix_hint.search(window)
            if m:
                hint_abs = window_start + m.start()
                boundary = min(boundary, hint_abs)
        return boundary

    def _settle_closed_fields(self, boundary: int) -> int:
        """已闭合字段若起点落在边界之前，则把边界推到其终点一并结算。

        条件：该字段必须已完全闭合（end <= safe_pos），保证其判定稳定。
        迭代处理连续多个字段。
        """
        changed = True
        while changed:
            changed = False
            for ev in self._field_events.values():
                if ev.start < boundary < ev.end and ev.end <= self._lex.safe_pos:
                    boundary = ev.end
                    changed = True
        return boundary

    def _pull_back_across_matches(self, boundary: int) -> int:
        """边界不得切过任何已完整到达的模式匹配。

        若某完整匹配起点 < boundary <= 终点，则把边界拉回该匹配起点
        （迭代到稳定），确保"部分原值"不可能被发出。
        """
        changed = True
        while changed:
            changed = False
            for rule in self.profile.pattern_rules:
                window, base = self._rule_window(rule)
                for m in rule.pattern.finditer(window):
                    s = m.start() + base
                    e = m.end() + base
                    if s < self._buf_off:
                        continue  # 跨入已发出区（回看字符），此前已处理
                    if s < boundary < e:
                        boundary = s
                        changed = True
        return boundary

    def _collect_candidates(self, lo: int, hi: int) -> list[Candidate]:
        """在绝对区间 [lo, hi) 内收集已完整出现的候选。"""
        cands: list[Candidate] = []
        for rule in self.profile.pattern_rules:
            window, base = self._rule_window(rule)
            for m in rule.pattern.finditer(window):
                if m.end() - m.start() == 0:
                    continue  # 防御零宽匹配
                s, e = m.start() + base, m.end() + base
                if s >= lo and e <= hi:
                    cands.append(Candidate(
                        rule_id=rule.id, start=s, end=e,
                        replacement=rule.replacement, priority=rule.priority,
                        source="pattern", matched_text=m.group(0)))
        fr = self.profile.field_rule
        if fr:
            for ev in self._field_events.values():
                # 字段可能在边界之前开始、刚在边界之前闭合：用 end<=hi 纳入，
                # 并保证其完整文本仍在当前缓冲内（start >= buf_off）。
                if ev.start >= self._buf_off and ev.end <= hi:
                    cands.append(Candidate(
                        rule_id=fr.id, start=ev.start, end=ev.end,
                        replacement=fr.replacement, priority=fr.priority,
                        source="field", key=ev.key,
                        matched_text=self._buf[ev.start - self._buf_off:
                                              ev.end - self._buf_off]))
        return cands

    def _scan_and_resolve(self, lo: int, hi: int):
        cands = self._collect_candidates(lo, hi)
        return resolve_overlaps(cands)

    def _settle_prefix(self, boundary: int) -> EmittedChunk:
        """对 [buf_off, boundary) 做最终裁决并发出；发出后丢弃已发缓冲。"""
        start_abs = self._buf_off
        if boundary <= start_abs:
            return EmittedChunk(self._chunk_index - 1, "",
                                start_abs, start_abs)
        accepted, rejected = self._scan_and_resolve(start_abs, boundary)
        newly_rejected = [r for r in rejected if r not in self._rejections]
        for rj in newly_rejected:
            self._rejections.append(rj)
            self._emit_event("OVERLAP_REJECTED", {
                "winner": rj.winner.rule_id,
                "loser": rj.loser.rule_id,
                "winner_span": [rj.winner.start, rj.winner.end],
                "loser_span": [rj.loser.start, rj.loser.end],
                "reason": rj.reason,
            })
        segment = self._buf[:boundary - self._buf_off]
        out = self._apply_candidates(segment, start_abs, accepted, flush=False)
        self._out_len += len(out)
        self._emitted_parts.append(out)
        # 更新边界回看：取被冲刷 segment 末尾的字符（原文，用于重建 \b）
        shift = boundary - self._buf_off
        keep_ctx = segment[max(0, shift - self._ctx_len):]
        if keep_ctx:
            self._ctx = keep_ctx
            self._ctx_off = boundary - len(keep_ctx)
        self._buf = self._buf[shift:]
        self._buf_off = boundary
        # 清理已随前缀离开缓冲（无论是否入选）的字段事件
        for st in [s for s, ev in self._field_events.items()
                   if ev.end <= boundary]:
            self._field_events.pop(st, None)
            self._settled_field_starts.discard(st)
        return EmittedChunk(self._chunk_index - 1, out, start_abs, boundary)

    def _apply_candidates(self, segment: str, seg_abs_off: int,
                          accepted: list[Candidate], *,
                          flush: bool,
                          output_base: int | None = None) -> str:
        """拼接单个 segment 的脱敏输出并登记映射。

        - segment 内输出位置从 0 起算（返回文本只含本 segment）；
        - 映射的全局输出坐标 = output_base + 局部位置。output_base 是本
          segment 之前已发出的输出字符数。settle 路径用内部 _out_len，
          finalize 路径显式传入当前 _out_len；本函数不修改 _out_len，
          由调用方在拿到返回值后累加，避免"基准+全文"重复计数。
        """
        base = self._out_len if output_base is None else output_base
        parts: list[str] = []
        local_out = 0
        cursor = 0
        for c in accepted:
            s = c.start - seg_abs_off
            e = c.end - seg_abs_off
            if s < cursor:
                continue  # 理论不应发生（候选互不重叠）
            gap = segment[cursor:s]
            parts.append(gap)
            local_out += len(gap)
            out_start = base + local_out
            parts.append(c.replacement)
            local_out += len(c.replacement)
            out_end = base + local_out
            original = segment[s:e]
            self._mappings.append(MappingRecord(
                rule_id=c.rule_id, source=c.source, key=c.key,
                original_start=c.start, original_end=c.end,
                output_start=out_start, output_end=out_end,
                replacement=c.replacement,
                original_sha256=_sha256(original),
                original_text=original,
            ))
            cursor = e
        parts.append(segment[cursor:])
        return "".join(parts)

    def _detect_ambiguous_tail(
        self, accepted: list[Candidate]
    ) -> list[tuple[int, int, str]]:
        """末尾窗口内像某规则不完整前缀、且未被完整替换覆盖的 token。

        日志常以 ``" <可疑数字> end"`` 结尾，故检查保留窗口内的每一个
        连续字母数字 token，而不仅是紧贴 EOF 的那个。token 起点必须在
        当前 buf 内（未发出过），且未被 accepted 完整覆盖。
        """
        import re
        findings: list[tuple[int, int, str]] = []
        if not self._buf:
            return findings
        full = self._ctx + self._buf
        base = self._ctx_off
        max_len = max((r.max_length for r in self.profile.pattern_rules),
                      default=0)
        win_lo = max(0, len(full) - max_len)
        window = full[win_lo:]
        for tok in re.finditer(r"[A-Za-z0-9]+", window):
            s_rel, e_rel = tok.span()
            s = base + win_lo + s_rel
            e = base + win_lo + e_rel
            if s < self._buf_off:
                continue  # 起点已发出，不能再判为尾部歧义
            if any(c.start <= s and c.end >= e for c in accepted):
                continue
            token = tok.group(0)
            # token 前一字符（在 full 中）若为字母数字/下划线，则该 token
            # 只是更长标识符的一部分，不按独立秘密前缀报警
            pre_idx = win_lo + s_rel - 1
            if pre_idx >= 0 and (full[pre_idx].isalnum()
                                 or full[pre_idx] == "_"):
                continue
            for rule in self.profile.pattern_rules:
                if rule.tail_suspicion is None:
                    continue
                if len(token) > rule.max_length:
                    continue
                if rule.tail_suspicion.fullmatch(token):
                    findings.append((s, e, rule.id))
                    break  # 同一 token 只归一条规则
        return findings

    def _verify_no_residuals(self, output: str,
                             accepted: list[Candidate]) -> list[str]:
        """残留自检：原候选原文不出现；模式在输出上零命中。"""
        findings: list[str] = []
        for c in accepted:
            if c.matched_text and c.matched_text in output:
                findings.append(
                    f"候选原文仍在输出中: rule={c.rule_id} "
                    f"span=[{c.start},{c.end})")
        for rule in self.profile.pattern_rules:
            for m in rule.pattern.finditer(output):
                findings.append(
                    f"模式 {rule.id} 在输出上仍命中: {m.group(0)!r} "
                    f"@[{m.start()},{m.end()})")
        return findings


def redact_whole(profile: Profile, text: str, *,
                 on_ambiguous_tail: str | None = None,
                 sink: EventSink | None = None) -> RedactionResult:
    """整段便捷入口：与流式同一条代码路径。"""
    r = StreamingRedactor(profile, sink=sink)
    r.feed(text)
    return r.finalize(on_ambiguous_tail=on_ambiguous_tail)


def _sha256(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
