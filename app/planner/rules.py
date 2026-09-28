"""Rule specification and per-rule compilation (pattern + template)."""

from __future__ import annotations

from dataclasses import dataclass

from ..engine import EngineOptions, compile_pattern
from ..engine.compiler import CompiledPattern
from ..template import Token, parse_template
from ..errors import InvalidRuleError

_MISSING_POLICIES = ("error", "empty")


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """One declared rewrite rule.

    Attributes
    ----------
    rule_id:
        Stable caller-provided identifier; unique within a ruleset.  It is the
        final tie-breaker when two rules have equal same-round priority, so
        plans are deterministic regardless of dict ordering.
    pattern / template:
        RE2 pattern and restricted replacement template.
    priority:
        Same-round priority; **higher wins** when matches overlap.  Rules at
        the same priority are resolved by rule_id.  The declared order of the
        rule list is otherwise irrelevant (documented contract).
    flags / longest_match / max_mem:
        Forwarded to the engine (:class:`~app.engine.EngineOptions`).
    missing_capture:
        ``"error"`` (default) fails the plan when an optional referenced group
        does not participate; ``"empty"`` substitutes empty bytes instead.
    """

    rule_id: str
    pattern: str
    template: str
    priority: int = 0
    flags: str = ""
    longest_match: bool = False
    max_mem: int = 8 * 1024 * 1024
    missing_capture: str = "error"

    def engine_options(self) -> EngineOptions:
        return EngineOptions(
            flags=self.flags,
            max_mem=self.max_mem,
            longest_match=self.longest_match,
        )


@dataclass(frozen=True, slots=True)
class CompiledRule:
    spec: RuleSpec
    compiled: CompiledPattern
    tokens: tuple[Token, ...]


def compile_rules(rules: list[RuleSpec]) -> list[CompiledRule]:
    """Validate a set of rules; fails closed on any bad rule.

    Rules are compiled eagerly so that rule creation never succeeds on a set
    that cannot run -- by the time a plan is built, patterns and templates are
    known-good and all capture references resolve.
    """
    if not rules:
        raise InvalidRuleError("ruleset must contain at least one rule")

    seen: set[str] = set()
    out: list[CompiledRule] = []
    for spec in rules:
        if not isinstance(spec, RuleSpec):
            raise InvalidRuleError(f"rule entries must be RuleSpec, got {type(spec).__name__}")
        if not spec.rule_id or not isinstance(spec.rule_id, str):
            raise InvalidRuleError("rule_id must be a non-empty string")
        if spec.rule_id in seen:
            raise InvalidRuleError("duplicate rule_id", rule_id=spec.rule_id)
        seen.add(spec.rule_id)
        if spec.missing_capture not in _MISSING_POLICIES:
            raise InvalidRuleError(
                f"missing_capture must be one of {_MISSING_POLICIES}",
                rule_id=spec.rule_id,
                value=spec.missing_capture,
            )
        if not isinstance(spec.priority, int):
            raise InvalidRuleError("priority must be an integer", rule_id=spec.rule_id)

        compiled = compile_pattern(spec.pattern, spec.engine_options())
        tokens = parse_template(spec.template, compiled)
        out.append(CompiledRule(spec=spec, compiled=compiled, tokens=tokens))
    return out
