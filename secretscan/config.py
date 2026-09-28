"""Rule / evidence parsing — versioned configuration packs.

Two independently versioned TOML packs are loaded:

* :class:`RulePack`  — structural rules with entropy thresholds (evidence)
* :class:`ScopePack` — scan limits and ignore patterns (coverage)

Both expose ``fingerprint()`` so every scan report can state exactly which
configuration version produced it, and :func:`load_settings` loads the small
runtime settings (database location, fingerprint pepper, HTTP roots).
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Pattern

_VALID_CONFIDENCE = frozenset({"low", "medium", "high"})


class ConfigError(ValueError):
    """A rule/scope/settings pack is missing fields or has invalid values."""


@dataclass(frozen=True)
class StructuralRule:
    """One structural secret rule (compiled evidence pattern)."""

    id: str
    description: str
    pattern: Pattern[str]
    raw_pattern: str
    secret_group: int = 0
    entropy_group: int = 0
    min_entropy: float = 0.0
    min_length: int = 0
    confidence: str = "low"
    multiline: bool = False
    require_keyword: bool = False
    keyword_pattern: Pattern[str] | None = None
    keyword_window: int = 80
    deny_substrings: frozenset[str] = frozenset()

    def validate_candidate(self, value: str, entropy: float) -> bool:
        """Apply this rule's non-structural gates (length, entropy, denylist)."""
        if len(value) < self.min_length:
            return False
        if entropy < self.min_entropy:
            return False
        lowered = value.lower()
        if any(bad in lowered for bad in self.deny_substrings):
            return False
        return True


@dataclass(frozen=True)
class RulePack:
    """All structural rules plus the pack's versioned identity."""

    name: str
    version: str
    rules: tuple[StructuralRule, ...]
    source: Path
    source_sha256: str

    def fingerprint(self) -> str:
        """Stable identity of the exact rule bytes used by a scan."""
        return f"rules:{self.version}@sha256:{self.source_sha256[:16]}"


def _compile_rule(raw: dict, source: Path) -> StructuralRule:
    required = ("id", "description", "pattern", "secret_group",
                "entropy_group", "min_entropy", "confidence")
    missing = [k for k in required if k not in raw]
    if missing:
        raise ConfigError(f"{source}: rule is missing fields {missing}")
    rid = str(raw["id"])
    flags = re.MULTILINE if raw.get("multiline", False) else 0
    try:
        pattern = re.compile(str(raw["pattern"]), flags)
    except re.error as exc:
        raise ConfigError(f"{source}: rule {rid!r} has invalid regex: {exc}") from exc
    confidence = str(raw["confidence"]).lower()
    if confidence not in _VALID_CONFIDENCE:
        raise ConfigError(
            f"{source}: rule {rid!r} confidence must be one of "
            f"{sorted(_VALID_CONFIDENCE)}")
    groups = pattern.groups
    secret_group = int(raw["secret_group"])
    entropy_group = int(raw["entropy_group"])
    for label, g in (("secret_group", secret_group), ("entropy_group", entropy_group)):
        if not 0 <= g <= groups:
            raise ConfigError(
                f"{source}: rule {rid!r} {label}={g} but pattern has "
                f"{groups} groups")
    keyword_pattern = None
    if raw.get("require_keyword", False):
        kp = raw.get("keyword_pattern")
        if not kp:
            raise ConfigError(
                f"{source}: rule {rid!r} require_keyword needs keyword_pattern")
        keyword_pattern = re.compile(str(kp))
    denies = frozenset(str(s).lower() for s in raw.get("deny_substrings", []))
    return StructuralRule(
        id=rid,
        description=str(raw["description"]),
        pattern=pattern,
        raw_pattern=str(raw["pattern"]),
        secret_group=secret_group,
        entropy_group=entropy_group,
        min_entropy=float(raw["min_entropy"]),
        min_length=int(raw.get("min_length", 0)),
        confidence=confidence,
        multiline=bool(raw.get("multiline", False)),
        require_keyword=bool(raw.get("require_keyword", False)),
        keyword_pattern=keyword_pattern,
        keyword_window=int(raw.get("keyword_window", 80)),
        deny_substrings=denies,
    )


def load_rule_pack(path: str | Path) -> RulePack:
    """Parse and validate a structural rule pack TOML file."""
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"rule pack not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    meta = data.get("meta")
    if not meta or "version" not in meta or "name" not in meta:
        raise ConfigError(f"{path}: [meta] name and version are required")
    raw_rules = data.get("rules")
    if not raw_rules:
        raise ConfigError(f"{path}: at least one [[rules]] entry is required")
    ids = [str(r.get("id")) for r in raw_rules]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ConfigError(f"{path}: duplicate rule ids: {dupes}")
    rules = tuple(_compile_rule(r, path) for r in raw_rules)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return RulePack(name=str(meta["name"]), version=str(meta["version"]),
                    rules=rules, source=path, source_sha256=digest)


# ---------------------------------------------------------------------------
# Scope / ignore pack
# ---------------------------------------------------------------------------

def _glob_to_regex(pattern: str) -> str:
    """Translate a gitignore-style glob (**, *, ?, [..]) to an anchored regex.

    A pattern without a slash (other than a possible trailing ``/``) matches at
    every directory depth (``foo`` behaves like ``**/foo``).
    """
    trailing_dir = pattern.endswith("/")
    p = pattern[:-1] if trailing_dir else pattern
    anchored = "/" in p
    i = 0
    out: list[str] = []
    while i < len(p):
        c = p[i]
        if c == "*":
            if i + 1 < len(p) and p[i + 1] == "*":
                # ** across path separators
                out.append(".*")
                i += 2
                if i < len(p) and p[i] == "/":
                    i += 1
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = p.find("]", i + 1)
            if j == -1:
                out.append(re.escape("["))
                i += 1
            else:
                out.append("[" + p[i + 1:j].replace("\\", "\\\\") + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    body = "".join(out)
    if trailing_dir:
        body += "(?:/|$)"
    prefix = "^" if anchored else r"(?:^|.*/)"
    suffix = "" if trailing_dir else "$"
    return prefix + body + suffix


@dataclass(frozen=True)
class ScopePack:
    """Versioned scan limits and ignore rules."""

    name: str
    version: str
    max_file_bytes: int
    binary_min_run: int
    ignore_patterns: tuple[str, ...]
    ignore_regexes: tuple[Pattern[str], ...] = field(repr=False)
    source: Path
    source_sha256: str

    def is_ignored(self, posix_relpath: str, is_dir: bool) -> bool:
        """Return True when a repo-relative path is intentionally ignored."""
        target = posix_relpath + "/" if is_dir else posix_relpath
        for rx in self.ignore_regexes:
            if rx.search(target):
                return True
        return False

    def fingerprint(self) -> str:
        return f"scope:{self.version}@sha256:{self.source_sha256[:16]}"


def load_scope_pack(path: str | Path) -> ScopePack:
    """Parse and validate a scope/ignore pack TOML file."""
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"scope pack not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    meta = data.get("meta")
    if not meta or "version" not in meta or "name" not in meta:
        raise ConfigError(f"{path}: [meta] name and version are required")
    limits = data.get("limits", {})
    max_file_bytes = int(limits.get("max_file_bytes", 1024 * 1024))
    if max_file_bytes <= 0:
        raise ConfigError(f"{path}: limits.max_file_bytes must be positive")
    binary_min_run = int(limits.get("binary_min_run", 8))
    if binary_min_run < 1:
        raise ConfigError(f"{path}: limits.binary_min_run must be >= 1")
    patterns = tuple(data.get("ignore", {}).get("patterns", []))
    regexes = tuple(re.compile(_glob_to_regex(p)) for p in patterns)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return ScopePack(
        name=str(meta["name"]), version=str(meta["version"]),
        max_file_bytes=max_file_bytes, binary_min_run=binary_min_run,
        ignore_patterns=patterns, ignore_regexes=regexes,
        source=path, source_sha256=digest)


# ---------------------------------------------------------------------------
# Runtime settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    """Runtime settings; paths resolved by the application/CLI layer.

    ``fingerprint_pepper`` must be supplied (env ``SECRETSCAN_PEPPER`` takes
    precedence). The bundled development value is public and only suitable for
    local fixtures — deployments MUST override it.
    """

    workspace_db: Path
    fingerprint_pepper: str
    pepper_id: str
    allowed_roots: tuple[Path, ...]
    log_file: Path | None = None


_DEFAULT_DEV_PEPPER = "dev-pepper-do-not-use-in-production-opp275"


def load_settings(
    db_path: str | Path,
    allowed_roots: tuple[str | Path, ...] | None = None,
    log_file: str | Path | None = None,
) -> Settings:
    """Assemble runtime settings from arguments plus environment overrides."""
    pepper = os.environ.get("SECRETSCAN_PEPPER", _DEFAULT_DEV_PEPPER)
    if not pepper.strip():
        raise ConfigError("fingerprint pepper must not be empty")
    pepper_id = hashlib.sha256(pepper.encode("utf-8")).hexdigest()[:12]
    roots = tuple(Path(p).resolve() for p in (allowed_roots or ()))
    for root in roots:
        if not root.exists() or not root.is_dir():
            raise ConfigError(f"allowed root does not exist or is not a dir: {root}")
    return Settings(
        workspace_db=Path(db_path),
        fingerprint_pepper=pepper,
        pepper_id=pepper_id,
        allowed_roots=roots,
        log_file=Path(log_file) if log_file else None,
    )
