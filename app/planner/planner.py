"""Build a non-overlapping, priority-resolved plan.

Same-round semantics (the contract)
-----------------------------------
1. Every rule is scanned over the **original** source buffer independently.
   Replacements are produced later, so replacement text can never be re-matched
   "in the same round" -- there is exactly one scan, over one buffer.
2. Rules are processed in declared same-round priority order (higher priority
   first; ``rule_id`` ascending as a deterministic tie-breaker).  Higher
   priority claims regions first.
3. Two candidates conflict iff:
   * their byte ranges share any covered byte (half-open intervals overlap:
     ``s1 < e2 and s2 < e1``), or
   * they start at the same offset.  Same-start conflicts cover all pairs,
     including zero-width vs zero-width and zero-width vs non-empty: at a given
     boundary only the winner may fire, which keeps the result unambiguous and
     matches "leftmost, then priority, then rule_id" intuition.
   A zero-width candidate strictly *inside* a claimed range (boundary between
   two covered bytes, or at its end boundary reached by later rules) is
   likewise suppressed because it fires at a position the winning rule already
   owns -- except that intervals are claimed by higher-priority rules and a
   later zero-width candidate at the exact *end* of a claimed interval does not
   share any covered byte and does not share a start, so it is allowed: the
   matches are adjacent, not overlapping.
4. Accepted edits are emitted in document order.  Zero-width edits insert
   bytes at one boundary and never consume input.
5. Adjacent non-empty matches (``end_i == start_j``) do not conflict: every
   source byte is still covered at most once.

Determinism does not depend on the order rules were supplied beyond the
documented (priority, rule_id) ordering.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from ..engine import scan
from ..engine.scanner import _BudgetOverflow, Candidate
from ..errors import (
    MatchBudgetExceededError,
    OutputBudgetExceededError,
)
from ..textspec import ByteIndex, sha256_hex, validate_byte_range
from ..template import render
from .model import Decision, Edit, Plan, PlanResult
from .rules import CompiledRule, compile_rules, RuleSpec


@dataclass(frozen=True, slots=True)
class PlannerLimits:
    max_candidates_per_rule: int = 200_000
    max_edits: int = 200_000
    max_output_bytes: int = 256 * 1024 * 1024


@dataclass(slots=True)
class _Claim:
    """Mutable per-build claim bookkeeping."""

    # Disjoint, sorted half-open byte intervals covered by accepted NON-EMPTY
    # edits. Parallel arrays used with bisect for O(log k) conflict checks.
    starts: list[int] = field(default_factory=list)
    ends: list[int] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)
    # Offsets at which ANY candidate (empty or non-empty) was accepted.  At one
    # offset at most one candidate may fire.
    points: dict[int, str] = field(default_factory=dict)

    def interval_conflict(self, s: int, e: int) -> str | None:
        """Owner of a claimed non-empty interval overlapping [s, e), if any.

        Claimed intervals are disjoint and sorted.  Half-open overlap is
        ``cs < e and s < ce``.  Only two neighbors can satisfy it: the
        predecessor (its ``ce`` may reach past ``s``, including containing it)
        and the successor (its ``cs`` may be strictly before ``e``).
        """
        i = bisect.bisect_right(self.starts, s) - 1
        if i >= 0 and s < self.ends[i]:  # predecessor covers/contains s
            return self.owners[i]
        j = i + 1
        if j < len(self.starts) and self.starts[j] < e:  # successor inside
            return self.owners[j]
        return None

    def add_interval(self, s: int, e: int, owner: str) -> None:
        i = bisect.bisect_left(self.starts, s)
        self.starts.insert(i, s)
        self.ends.insert(i, e)
        self.owners.insert(i, owner)


def build_plan(
    data: bytes,
    rules: list[RuleSpec] | list[CompiledRule],
    *,
    normalize_newlines: bool = False,
    index: ByteIndex | None = None,
    limits: PlannerLimits | None = None,
) -> PlanResult:
    """Compile (if needed), scan, resolve, render. Pure function of inputs.

    Raises structured errors from :mod:`app.errors`.  On success the returned
    plan is fully resolved: each edit's replacement bytes are final and every
    referenced capture has been validated against this concrete text.
    """
    limits = limits or PlannerLimits()
    compiled = compile_rules(rules) if (rules and isinstance(rules[0], RuleSpec)) else rules  # type: ignore[arg-type]
    index = index or ByteIndex(data)
    decisions: list[Decision] = []
    claim = _Claim()
    accepted: list[tuple[int, int, CompiledRule, Candidate, bytes]] = []
    total_candidates = 0

    ordered = sorted(
        compiled,  # type: ignore[arg-type]
        key=lambda r: (-r.spec.priority, r.spec.rule_id),
    )

    for rule in ordered:
        try:
            cands = scan(
                rule.compiled,
                data,
                index,
                max_matches=limits.max_candidates_per_rule,
            )
        except _BudgetOverflow as exc:
            raise MatchBudgetExceededError(
                "candidate fan-out exceeded per-rule cap",
                rule_id=rule.spec.rule_id,
                observed=exc.count,
                limit=limits.max_candidates_per_rule,
            ) from exc

        # Per-rule candidates are already in document order from the scanner.
        for cand in cands:
            total_candidates += 1
            # Contract: validate raw byte ranges before rendering captures.
            validate_byte_range(data, cand.start, cand.end, index=index)
            for g in cand.groups:
                if g.start is not None:
                    validate_byte_range(data, g.start, g.end or g.start, index=index)

            owner = _conflict_owner(claim, cand)
            if owner is not None:
                decisions.append(
                    Decision(
                        stage="reject",
                        rule_id=rule.spec.rule_id,
                        start=cand.start,
                        end=cand.end,
                        zero_width=cand.zero_width,
                        conflicts_with=owner,
                        reason=_reject_reason(claim, cand, owner),
                    )
                )
                continue

            rendered = render(
                rule.tokens,
                cand,
                missing_capture=rule.spec.missing_capture,
            )  # raises CaptureMissingError under the default policy

            accepted.append((cand.start, cand.end, rule, cand, rendered.output))
            claim.points[cand.start] = rule.spec.rule_id
            if cand.end > cand.start:
                claim.add_interval(cand.start, cand.end, rule.spec.rule_id)
            decisions.append(
                Decision(
                    stage="accept",
                    rule_id=rule.spec.rule_id,
                    start=cand.start,
                    end=cand.end,
                    zero_width=cand.zero_width,
                    reason="highest-priority candidate at leftmost position",
                )
            )
            if len(accepted) > limits.max_edits:
                raise MatchBudgetExceededError(
                    "accepted edit count exceeded cap",
                    observed=len(accepted),
                    limit=limits.max_edits,
                )

    accepted.sort(key=lambda t: (t[0], t[1]))
    # Final projected size is a deterministic function of the accepted edits;
    # evaluate it once so the reported projection is not scan-order dependent.
    projected = len(data) + sum(
        len(repl) - (e - s) for s, e, _r, _c, repl in accepted
    )
    if projected > limits.max_output_bytes:
        raise OutputBudgetExceededError(
            "rendered output would exceed size ceiling",
            projected=projected,
            limit=limits.max_output_bytes,
        )
    edits: list[Edit] = [
        Edit(
            start=s,
            end=e,
            replacement=repl,
            rule_id=rule.spec.rule_id,
            matched=bytes(data[s:e]),
            zero_width=(s == e),
        )
        for s, e, rule, _cand, repl in accepted
    ]

    plan = Plan(
        source_sha256=sha256_hex(data),
        source_length=len(data),
        normalize_newlines=normalize_newlines,
        edits=tuple(edits),
        rule_ids=tuple(r.spec.rule_id for r in ordered),
    )
    dropped = total_candidates - len(edits)
    return PlanResult(
        plan=plan,
        decisions=decisions,
        candidates_total=total_candidates,
        candidates_dropped=dropped,
    )


def _conflict_owner(claim: _Claim, cand: Candidate) -> str | None:
    point_owner = claim.points.get(cand.start)
    if point_owner is not None:
        return point_owner
    # For non-empty candidates the interval test covers byte overlap.
    if cand.end > cand.start:
        return claim.interval_conflict(cand.start, cand.end)
    # A zero-width candidate is a single point.  It conflicts if that point
    # lies *inside* a claimed non-empty range (cs < point < ce): the winning
    # rule owns every boundary between -- and strictly within -- the bytes it
    # covers.  A point exactly at an end boundary (point == ce) is adjacent and
    # is allowed.
    i = bisect.bisect_right(claim.starts, cand.start) - 1
    if i >= 0 and cand.start < claim.ends[i]:
        return claim.owners[i]
    return None


def _reject_reason(claim: _Claim, cand: Candidate, owner: str) -> str:
    if claim.points.get(cand.start) is not None:
        return "same start offset already claimed by higher/earlier-priority rule"
    return "byte range overlaps an already claimed non-empty match"
