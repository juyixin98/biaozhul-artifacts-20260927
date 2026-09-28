"""Security kernel: evidence classification, overlap arbitration, offsets.

The kernel never logs or returns originals beyond the caller's own input.
It produces:

* ``redacted`` output text,
* ``mappings`` — an auditable position map linking every replaced span back
  to the original coordinates (the original fragment itself is stored only
  via the encrypted audit layer),
* ``uncertain`` — fragments that *look* secret but failed an evidence check,
  listed separately rather than silently released or confidently redacted.

Streaming property (:class:`StreamRedactor`): no text is ever emitted while it
could still be the prefix of a longer match starting inside or across the
chunk boundary. The last ``max_len`` characters stay buffered, so a token
split across chunks is never released in pieces.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

from .rules import Rule, RuleKind, RuleSet, RawMatch, luhn_ok, cn_id_ok


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    rule_id: str
    label: str
    priority: int
    original: str
    replacement: str
    uncertain: bool = False
    reason: Optional[str] = None

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def overlaps(self, other: "Span") -> bool:  # type: ignore[override]
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class MappingRecord:
    rule_id: str
    label: str
    # Coordinates in the ORIGINAL stream (character offsets, code points).
    original_start: int
    original_end: int
    # Coordinates in the REDACTED stream.
    output_start: int
    output_end: int
    original_length: int
    replaced_length: int
    uncertain: bool
    original_sha256: str
    reason: Optional[str] = None


@dataclass
class RedactionResult:
    redacted: str
    mappings: list[MappingRecord] = field(default_factory=list)
    uncertain: list[Span] = field(default_factory=list)
    input_length: int = 0
    output_length: int = 0


# ---------------------------------------------------------------------------
# Candidate collection + evidence
# ---------------------------------------------------------------------------


def _spans_for_text(text: str, rules: tuple[Rule, ...], offset: int) -> tuple[list[Span], list[Span]]:
    definite: list[Span] = []
    uncertain: list[Span] = []
    for rule in rules:
        for raw in rule.finditer(text):
            evidence = text[raw.whole_start : raw.whole_end]
            if rule.kind is RuleKind.FIELD:
                evidence_value = raw.text
            else:
                evidence_value = raw.text
            if rule.validator(evidence_value):
                definite.append(
                    Span(
                        start=offset + raw.start,
                        end=offset + raw.end,
                        rule_id=rule.rule_id,
                        label=rule.label,
                        priority=rule.priority,
                        original=text[raw.start : raw.end],
                        replacement=rule.redaction_token(),
                    )
                )
            elif rule.uncertainty_on_fail:
                uncertain.append(
                    Span(
                        start=offset + raw.start,
                        end=offset + raw.end,
                        rule_id=rule.rule_id,
                        label=f"{rule.label}?",
                        priority=rule.priority,
                        original=text[raw.start : raw.end],
                        replacement="[REDACTED:?]",
                        uncertain=True,
                        reason=f"evidence_check_failed:{rule.validator_name}",
                    )
                )
    return definite, uncertain


# ---------------------------------------------------------------------------
# Overlap resolution with explicit priority semantics
# ---------------------------------------------------------------------------


def resolve_overlaps(spans: list[Span]) -> list[Span]:
    """Greedy priority arbitration over overlapping candidates.

    Higher ``priority`` wins. Within one overlap cluster the winner is the
    span with the highest priority; ties are broken by earliest start, then
    longest span, then rule id (fully deterministic). Spans merely adjacent
    (``a.end == b.start``) never conflict and are both kept, so two
    neighboring secrets are both redacted.
    """
    ordered = sorted(
        spans,
        key=lambda s: (-s.priority, s.start, -s.length, s.rule_id),
    )
    accepted: list[Span] = []
    for cand in ordered:
        if any(
            cand.start < kept.end and kept.start < cand.end for kept in accepted
        ):
            continue
        accepted.append(cand)
    return sorted(accepted, key=lambda s: (s.start, s.end))


# ---------------------------------------------------------------------------
# Output + offset map construction
# ---------------------------------------------------------------------------


def build_output(
    text: str,
    spans: list[Span],
    offset_base: int = 0,
    output_base: int = 0,
) -> tuple[str, list[MappingRecord]]:
    """Render redacted text and the original<->output position map.

    ``offset_base`` shifts the *original* coordinates (used when the text is a
    window of a larger stream); ``output_base`` shifts the output coordinates.
    """
    parts: list[str] = []
    mappings: list[MappingRecord] = []
    cursor = 0
    out_cursor = output_base
    for span in sorted(spans, key=lambda s: s.start):
        local_start = span.start - offset_base
        local_end = span.end - offset_base
        parts.append(text[cursor:local_start])
        out_cursor += local_start - cursor
        replacement = span.replacement
        parts.append(replacement)
        digest = hashlib.sha256(span.original.encode("utf-8")).hexdigest()
        mappings.append(
            MappingRecord(
                rule_id=span.rule_id,
                label=span.label,
                original_start=span.start,
                original_end=span.end,
                output_start=out_cursor,
                output_end=out_cursor + len(replacement),
                original_length=span.length,
                replaced_length=len(replacement),
                uncertain=span.uncertain,
                original_sha256=digest,
                reason=span.reason,
            )
        )
        out_cursor += len(replacement)
        cursor = local_end
    parts.append(text[cursor:])
    out_cursor += len(text) - cursor
    return "".join(parts), mappings


# ---------------------------------------------------------------------------
# Uncertainty heuristics (run only on what would otherwise be released)
# ---------------------------------------------------------------------------

_DIGIT_RUN = re.compile(r"\d{13,19}")
_PREFIX_TOKEN = re.compile(
    r"\b(?:sk|pk|tok|api|key)(?:_(?:live|test|syn))?_?[A-Za-z0-9]{4,11}(?![A-Za-z0-9])"
)


def scan_uncertain(text: str, protected: list[Span], offset: int = 0) -> list[Span]:
    """Find suspicious fragments inside *unprotected* regions.

    These are reported, not auto-replaced:

    * 13–19 digit runs that fail Luhn (shape of a card number, bad checksum —
      typo, truncated token, or non-secret),
    * 18-digit runs failing the resident-ID checksum,
    * ``sk_``/``tok_``-style prefixes with too few body characters to meet
      the strict token rule (possible truncated / partially copied token).
    """
    bounds = sorted((s.start - offset, s.end - offset) for s in protected)
    findings: list[Span] = []

    def covered(i: int, j: int) -> bool:
        return any(i < e and b < j for b, e in bounds)

    for m in _DIGIT_RUN.finditer(text):
        if covered(m.start(), m.end()):
            continue
        frag = m.group(0)
        reason = None
        label = None
        card_shape = len(frag) in (16, 17, 18, 19) and (
            frag.startswith("62") or frag.startswith("4")
        )
        if card_shape and not luhn_ok(frag):
            reason, label = "evidence_check_failed:luhn", "bank_card?"
        elif len(frag) == 18 and not cn_id_ok(frag):
            reason, label = "evidence_check_failed:cn_id_checksum", "cn_id_card?"
        if reason:
            findings.append(
                Span(
                    start=offset + m.start(),
                    end=offset + m.end(),
                    rule_id="heuristic.digit_run",
                    label=label or "digit_run",
                    priority=0,
                    original=frag,
                    replacement="[REDACTED:?]",
                    uncertain=True,
                    reason=reason,
                )
            )
    for m in _PREFIX_TOKEN.finditer(text):
        if covered(m.start(), m.end()):
            continue
        frag = m.group(0)
        findings.append(
            Span(
                start=offset + m.start(),
                end=offset + m.end(),
                rule_id="heuristic.token_prefix",
                label="api_token?",
                priority=0,
                original=frag,
                replacement="[REDACTED:?]",
                uncertain=True,
                reason="truncated_token_prefix",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Whole-document redaction
# ---------------------------------------------------------------------------


def redact_full(text: str, ruleset: RuleSet) -> RedactionResult:
    definite, failed = _spans_for_text(text, ruleset.rules, 0)
    accepted = resolve_overlaps(definite)
    accepted_bounds = accepted  # accepted spans are already global
    heuristic = scan_uncertain(text, accepted_bounds, 0)
    uncertain = resolve_overlaps(failed + heuristic)
    # Uncertain spans inside an accepted definite span are meaningless.
    uncertain = [
        u
        for u in uncertain
        if not any(u.start < a.end and a.start < u.end for a in accepted)
    ]
    redacted, mappings = build_output(text, accepted, 0, 0)
    return RedactionResult(
        redacted=redacted,
        mappings=mappings,
        uncertain=uncertain,
        input_length=len(text),
        output_length=len(redacted),
    )


# ---------------------------------------------------------------------------
# Streaming state machine
# ---------------------------------------------------------------------------


@dataclass
class _Pending:
    """A definite span found inside the retained buffer this round."""

    start: int
    end: int
    span: Span


class StreamRedactor:
    """Chunk-wise redactor whose output is identical to ``redact_full``.

    Invariant: the concatenation of every emitted chunk plus the final flush
    equals ``redact_full(concatenation_of_inputs).redacted``.
    """

    def __init__(self, ruleset: RuleSet) -> None:
        self.ruleset = ruleset
        self._holdback = ruleset.max_len
        self._buf = ""
        self._buf_start = 0  # global offset of buffer index 0
        self._out_pos = 0
        self.mappings: list[MappingRecord] = []
        self._pending_originals: dict[tuple[str, int, int], str] = {}
        self._drained_mappings = 0
        self.uncertain: list[Span] = []
        self.input_length = 0
        self._emitted_span_keys: set[tuple[str, int, int]] = set()
    def push(self, chunk: str) -> str:
        self._buf += chunk
        self.input_length += len(chunk)
        if len(self._buf) <= self._holdback:
            # Any point in the buffer may still be extended by a future chunk.
            return ""
        return self._emit(final=False)

    def finish(self) -> str:
        out = self._emit(final=True)
        self._buf = ""
        return out

    # -- internals --------------------------------------------------------

    def _collect(self, text: str, base: int) -> tuple[list[Span], list[Span]]:
        return _spans_for_text(text, self.ruleset.rules, base)

    def _emit(self, final: bool) -> str:
        definite, failed = self._collect(self._buf, self._buf_start)
        accepted = resolve_overlaps(definite)

        if final:
            cut = len(self._buf)
        else:
            # Safe cut: at least max_len characters must remain. Shrink it
            # leftwards past any span that straddles the cut (emitting half a
            # match would leak the remainder on the next push).
            cut0 = len(self._buf) - self._holdback
            cut = cut0
            changed = True
            while changed:
                changed = False
                for s in accepted:
                    if s.start < self._buf_start + cut < s.end:
                        cut = s.start - self._buf_start
                        changed = True
            if cut <= 0:
                return ""

        window = self._buf[:cut]
        window_spans = [s for s in accepted if s.end <= self._buf_start + cut]

        out_parts: list[str] = []
        cursor = 0
        for span in window_spans:
            ls = span.start - self._buf_start
            le = span.end - self._buf_start
            out_parts.append(window[cursor:ls])
            self._out_pos += ls - cursor
            key = (span.rule_id, span.start, span.end)
            if key not in self._emitted_span_keys:
                digest = hashlib.sha256(span.original.encode()).hexdigest()
                self.mappings.append(
                    MappingRecord(
                        rule_id=span.rule_id,
                        label=span.label,
                        original_start=span.start,
                        original_end=span.end,
                        output_start=self._out_pos,
                        output_end=self._out_pos + len(span.replacement),
                        original_length=span.length,
                        replaced_length=len(span.replacement),
                        uncertain=False,
                        original_sha256=digest,
                    )
                )
                self._emitted_span_keys.add(key)
                # Cache the original until the audit layer has persisted it;
                # the span text is about to leave the buffer forever.
                self._pending_originals[key] = span.original
            out_parts.append(span.replacement)
            self._out_pos += len(span.replacement)
            cursor = le
        out_parts.append(window[cursor:])
        self._out_pos += len(window) - cursor

        # Evidence failures fully contained in a window that is leaving
        # forever become "uncertain" findings on that window's plaintext.
        # On the final flush every remaining failed candidate is considered.
        if final:
            leaving_failed = [
                s
                for s in failed
                if not any(
                    s.start < a.end and a.start < s.end for a in window_spans
                )
            ]
        else:
            leaving_failed = [
                s
                for s in failed
                if s.end <= self._buf_start + cut
                and not any(
                    s.start < a.end and a.start < s.end for a in window_spans
                )
            ]
        heur = scan_uncertain(window, window_spans, self._buf_start)
        uncertain_new = resolve_overlaps(leaving_failed + heur)
        uncertain_new = [
            u
            for u in uncertain_new
            if not any(u.start < a.end and a.start < u.end for a in window_spans)
        ]
        self._merge_uncertain(uncertain_new)

        self._buf = self._buf[cut:]
        self._buf_start += cut
        return "".join(out_parts)

    def _merge_uncertain(self, new: list[Span]) -> None:
        existing = {(u.rule_id, u.start, u.end) for u in self.uncertain}
        for u in new:
            key = (u.rule_id, u.start, u.end)
            if key not in existing:
                self.uncertain.append(u)
                existing.add(key)

    def result_snapshot(self) -> RedactionResult:
        return RedactionResult(
            redacted="",
            mappings=list(self.mappings),
            uncertain=list(self.uncertain),
            input_length=self.input_length,
            output_length=self._out_pos,
        )

    def drain_new_mappings(
        self,
    ) -> tuple[list[MappingRecord], dict[tuple[int, int], str]]:
        """Return mappings not yet drained plus their originals.

        Callers persist these to the audit store; originals are forgotten on
        the next drain.
        """
        fresh = self.mappings[self._drained_mappings :]
        originals: dict[tuple[int, int], str] = {}
        for m in fresh:
            key = (m.rule_id, m.original_start, m.original_end)
            if key in self._pending_originals:
                originals[(m.original_start, m.original_end)] = (
                    self._pending_originals.pop(key)
                )
        self._drained_mappings = len(self.mappings)
        return fresh, originals

    def original_at(self, start: int, end: int) -> Optional[str]:
        """Return the still-buffered original text for a global span."""
        ls, le = start - self._buf_start, end - self._buf_start
        if ls < 0 or le > len(self._buf):
            return None
        return self._buf[ls:le]
