"""Versioned rule / scope / ignore configuration.

A config bundle is an immutable, validated object. The raw YAML digest is
retained so each scan can record exactly which configuration content was used;
``rules_version`` is the human-facing, operator-bumped version string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .errors import ConfigError
from .fingerprint import content_digest

SUPPORTED_SCHEMA = "1.0"
RULE_PREFIX_SOURCES = ("rule",)


@dataclass(frozen=True)
class Rule:
    id: str
    description: str
    category: str
    confidence: str  # high | medium | low
    pattern: str
    compiled: re.Pattern[bytes]
    min_entropy: float | None


@dataclass(frozen=True)
class EntropyConfig:
    shannon_threshold: float
    min_token_length: int


@dataclass(frozen=True)
class ScopeConfig:
    max_file_bytes: int
    follow_symlinks: bool


@dataclass(frozen=True)
class RuleSet:
    schema_version: str
    rules_version: str
    classification_version: str
    scope: ScopeConfig
    ignore_patterns: tuple[str, ...]
    entropy: EntropyConfig
    rules: tuple[Rule, ...]
    raw_digest: str
    source_path: str | None = None

    def rule_by_id(self, rule_id: str) -> Rule | None:
        for r in self.rules:
            if r.id == rule_id:
                return r
        return None


def _require(d: dict, key: str, ctx: str) -> object:
    if key not in d:
        raise ConfigError(f"missing required key '{key}' in {ctx}")
    return d[key]


def _parse_rule(raw: dict, ctx: str, entropy_floor: float) -> Rule:
    rid = _require(raw, "id", ctx)
    if not isinstance(rid, str) or not re.fullmatch(r"[a-z0-9_]{2,64}", rid):
        raise ConfigError(f"{ctx}: rule id must match [a-z0-9_]{{2,64}}, got {rid!r}")
    pattern = _require(raw, "pattern", f"rule {rid}")
    if not isinstance(pattern, str):
        raise ConfigError(f"rule {rid}: pattern must be a string")
    try:
        compiled = re.compile(pattern.encode("utf-8"))
    except re.error as exc:
        raise ConfigError(f"rule {rid}: invalid regex: {exc}") from exc
    if "secret" not in compiled.groupindex:
        raise ConfigError(f"rule {rid}: pattern must define a named group (?P<secret>...)")
    confidence = raw.get("confidence", "medium")
    if confidence not in ("high", "medium", "low"):
        raise ConfigError(f"rule {rid}: confidence must be high|medium|low, got {confidence!r}")
    min_entropy = raw.get("min_entropy")
    if min_entropy is not None:
        if not isinstance(min_entropy, (int, float)) or not 0.0 <= float(min_entropy) <= 8.0:
            raise ConfigError(f"rule {rid}: min_entropy must be between 0 and 8 bits/byte")
        min_entropy = float(min_entropy)
        if min_entropy > entropy_floor:
            raise ConfigError(
                f"rule {rid}: per-rule min_entropy {min_entropy} is above the global "
                f"heuristic threshold {entropy_floor}; the structural match would be "
                "downgraded by entropy logic, which is not allowed"
            )
    return Rule(
        id=rid,
        description=str(raw.get("description", "")),
        category=str(raw.get("category", "uncategorized")),
        confidence=confidence,
        pattern=pattern,
        compiled=compiled,
        min_entropy=min_entropy,
    )


def load_rules(path: str | Path) -> RuleSet:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"rule file not found: {p}")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read rule file {p}: {exc}") from exc
    return parse_rules(text, source_path=str(p))


def parse_rules(text: str, *, source_path: str | None = None) -> RuleSet:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"rule file is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("rule file top level must be a mapping")

    schema = str(_require(data, "schema_version", "root"))
    if schema != SUPPORTED_SCHEMA:
        raise ConfigError(f"unsupported schema_version {schema!r}; supported: {SUPPORTED_SCHEMA}")
    rules_version = str(_require(data, "rules_version", "root"))
    if not re.fullmatch(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}([.\-+][A-Za-z0-9.]+)?", rules_version):
        raise ConfigError(
            f"rules_version {rules_version!r} must be a date-based version YYYY.MM.DD[.suffix]"
        )
    classification_version = str(_require(data, "classification_version", "root"))

    scope_raw = _require(data, "scope", "root")
    if not isinstance(scope_raw, dict):
        raise ConfigError("scope must be a mapping")
    max_bytes = _require(scope_raw, "max_file_bytes", "scope")
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ConfigError("scope.max_file_bytes must be a positive integer")
    follow_symlinks = bool(scope_raw.get("follow_symlinks", False))

    ignore_raw = _require(data, "ignore", "root")
    patterns = tuple(ignore_raw.get("patterns", ()) or ())
    if not all(isinstance(x, str) for x in patterns):
        raise ConfigError("ignore.patterns must be a list of strings")

    entropy_raw = _require(data, "entropy", "root")
    threshold = float(_require(entropy_raw, "shannon_threshold", "entropy"))
    if not 0.0 <= threshold <= 8.0:
        raise ConfigError("entropy.shannon_threshold must be between 0 and 8 bits/byte")
    min_token = _require(entropy_raw, "min_token_length", "entropy")
    if not isinstance(min_token, int) or min_token < 8:
        raise ConfigError("entropy.min_token_length must be an integer >= 8")

    rules_raw = _require(data, "rules", "root")
    if not isinstance(rules_raw, list) or not rules_raw:
        raise ConfigError("rules must be a non-empty list")
    rules: list[Rule] = []
    seen: set[str] = set()
    for item in rules_raw:
        if not isinstance(item, dict):
            raise ConfigError("every rule entry must be a mapping")
        rule = _parse_rule(item, f"rules[{len(rules)}]", threshold)
        if rule.id in seen:
            raise ConfigError(f"duplicate rule id: {rule.id}")
        seen.add(rule.id)
        rules.append(rule)

    return RuleSet(
        schema_version=schema,
        rules_version=rules_version,
        classification_version=classification_version,
        scope=ScopeConfig(max_file_bytes=max_bytes, follow_symlinks=follow_symlinks),
        ignore_patterns=patterns,
        entropy=EntropyConfig(shannon_threshold=threshold, min_token_length=min_token),
        rules=tuple(rules),
        raw_digest=content_digest(text.encode("utf-8")),
        source_path=source_path,
    )
