"""Pure scan engine: a repository snapshot -> secret candidates.

The engine does no I/O against databases and performs no network access. It
walks a root directory, applies the versioned scope rules, classifies each
file, runs the versioned structural rules plus entropy gates, and returns
candidate objects. Lifecycle comparison (new / known-fixed / moved) lives in
:mod:`secretscan.service`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import media as media_mod
from .config import RulePack, ScopePack, StructuralRule
from .entropy import shannon_entropy
from .security import Fingerprinter, Secret, file_sha256, redacted_preview

# Inventory file statuses. Anything but "scanned" means the file's contents
# were NOT examined and the report must say so explicitly.
STATUS_SCANNED = "scanned"
STATUS_IGNORED = "ignored"
STATUS_OVERSIZE = "oversize"
STATUS_UNREADABLE = "unreadable"
STATUS_SYMLINK = "symlink-skipped"


@dataclass(frozen=True)
class FileInventory:
    """Coverage record for one path in the snapshot."""

    relpath: str
    size: int
    status: str
    media: str | None = None
    reason: str | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class Candidate:
    """One rule hit. Never a confirmed leak — a candidate for human review.

    Persistable fields contain no full secret: ``mask`` and keyed
    ``fingerprint`` only. ``secret`` is the transient raw value, excluded from
    every serialization (see :meth:`public_dict`).
    """

    relpath: str
    rule_id: str
    confidence: str  # low | medium | high — strength of the shape evidence
    secret: Secret
    mask: str
    fingerprint: str
    entropy: float
    line: int | None  # 1-based for text; None for matches inside binaries
    column: int       # 1-based text column; absolute byte offset+1 in binaries
    end_line: int | None
    end_column: int
    evidence_masked: str
    content_media: str  # "text" | "binary"
    file_sha256: str
    file_size: int

    def public_dict(self) -> dict:
        """Serialization shape — deliberately omits the raw ``secret``."""
        return {
            "relpath": self.relpath,
            "rule_id": self.rule_id,
            "confidence": self.confidence,
            "mask": self.mask,
            "fingerprint": self.fingerprint,
            "entropy": round(self.entropy, 4),
            "line": self.line,
            "column": self.column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "evidence": self.evidence_masked,
            "content_media": self.content_media,
            "file_sha256": self.file_sha256,
            "file_size": self.file_size,
        }


@dataclass
class EngineResult:
    root: str
    inventory: list[FileInventory] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)

    def by_status(self, status: str) -> list[FileInventory]:
        return [f for f in self.inventory if f.status == status]

    def unscanned(self) -> list[FileInventory]:
        """Files whose contents were not examined, with explicit reasons."""
        return [f for f in self.inventory
                if f.status in (STATUS_OVERSIZE, STATUS_UNREADABLE, STATUS_SYMLINK)]


def _line_col(text: str, offset: int) -> tuple[int, int]:
    """1-based (line, column) for a character offset in ``text``."""
    line = text.count("\n", 0, offset) + 1
    line_start = text.rfind("\n", 0, offset) + 1
    return line, offset - line_start + 1


def _keyword_present(rule: StructuralRule, text: str, start: int, end: int) -> bool:
    """Check the rule's keyword within a same-line window around the match."""
    if rule.keyword_pattern is None:
        return True
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    lo = max(line_start, start - rule.keyword_window)
    hi = min(line_end, end + rule.keyword_window)
    return rule.keyword_pattern.search(text, lo, hi) is not None


def _scan_text_unit(
    unit: str,
    rules: RulePack,
    fingerprinter: Fingerprinter,
    *,
    offset_base: int = 0,
    line_override: int | None = None,
) -> list[tuple[int, int, str, float, StructuralRule]]:
    """Run every rule over one text unit, resolving overlapping matches.

    Returns accepted ``(abs_start, abs_end, value, entropy, rule)`` tuples.
    Overlap resolution: earliest start wins; on ties the longest match, then
    the earlier (higher-priority) rule in the pack.
    """
    hits: list[tuple[int, int, str, float, StructuralRule]] = []
    for rule in rules.rules:
        for m in rule.pattern.finditer(unit):
            try:
                value = m.group(rule.secret_group)
                entropy_text = m.group(rule.entropy_group)
            except IndexError:
                continue
            if value is None:
                continue
            entropy = shannon_entropy(entropy_text or value)
            if not rule.validate_candidate(value, entropy):
                continue
            if not _keyword_present(rule, unit, m.start(), m.end()):
                continue
            # Overlap resolution works on the *secret* span, not the whole
            # match (a generic rule's keyword may start much earlier than the
            # secret a specialised rule also matches).
            hits.append((m.start(rule.secret_group), m.end(rule.secret_group),
                         value, entropy, rule))
    priority = {rule: i for i, rule in enumerate(rules.rules)}
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0]), priority[h[4]]))
    accepted: list[tuple[int, int, str, float, StructuralRule]] = []
    cursor = -1
    for start, end, value, entropy, rule in hits:
        if start < cursor:
            continue  # overlaps an already-accepted stronger hit
        accepted.append((start + offset_base, end + offset_base,
                         value, entropy, rule))
        cursor = end
    return accepted


def _build_candidate(
    relpath: str,
    content: str,
    abs_start: int,
    abs_end: int,
    value: str,
    entropy: float,
    rule: StructuralRule,
    fingerprinter: Fingerprinter,
    content_media: str,
    file_sha: str,
    file_size: int,
    *,
    line_override: int | None = None,
) -> Candidate:
    if line_override is None and content_media == "text":
        line, column = _line_col(content, abs_start)
        end_line, end_column = _line_col(content, max(abs_start, abs_end - 1))
        end_column += 1 if abs_end > abs_start else 0
    else:
        # Binary printable run: meaningful position is the byte offset.
        line, end_line = None, None
        column = abs_start + 1
        end_column = abs_end + 1
    evidence = redacted_preview(content, abs_start, abs_end)
    secret = Secret(value)
    return Candidate(
        relpath=relpath, rule_id=rule.id, confidence=rule.confidence,
        secret=secret, mask=secret.mask,
        fingerprint=fingerprinter.fingerprint(value),
        entropy=entropy, line=line, column=column,
        end_line=end_line, end_column=end_column,
        evidence_masked=evidence, content_media=content_media,
        file_sha256=file_sha, file_size=file_size)


def scan_file(
    relpath: str,
    data: bytes,
    rules: RulePack,
    scope: ScopePack,
    fingerprinter: Fingerprinter,
) -> tuple[list[Candidate], str, str]:
    """Scan one file's bytes. Returns ``(candidates, media_kind, reason)``."""
    kind = media_mod.classify(data)
    file_sha = file_sha256(data)
    candidates: list[Candidate] = []
    if kind.kind == "text":
        text = data.decode("utf-8")
        hits = _scan_text_unit(text, rules, fingerprinter)
        for start, end, value, entropy, rule in hits:
            candidates.append(_build_candidate(
                relpath, text, start, end, value, entropy, rule,
                fingerprinter, "text", file_sha, len(data)))
    else:
        for run, offset in media_mod.printable_runs(data, scope.binary_min_run):
            unit = run.decode("ascii")
            hits = _scan_text_unit(unit, rules, fingerprinter,
                                   offset_base=offset, line_override=0)
            for start, end, value, entropy, rule in hits:
                # Evidence preview must be taken within the run's own text.
                local_start, local_end = start - offset, end - offset
                candidates.append(_build_candidate(
                    relpath, unit, local_start, local_end, value, entropy,
                    rule, fingerprinter, "binary", file_sha, len(data)))
    return candidates, kind.kind, kind.reason


def _iter_snapshot(root: Path, scope: ScopePack):
    """Walk ``root`` without following symlinks, pruning ignored directories."""
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        kept_dirs: list[str] = []
        for d in dirnames:
            rel = os.path.relpath(os.path.join(dirpath, d), root).replace(os.sep, "/")
            if scope.is_ignored(rel + "/", is_dir=True):
                # Recorded in the inventory so pruning itself is auditable.
                yield rel + "/", True, None
            else:
                kept_dirs.append(d)
        dirnames[:] = kept_dirs
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            yield rel, False, full


def scan_snapshot(
    root: str | Path,
    rules: RulePack,
    scope: ScopePack,
    fingerprinter: Fingerprinter,
) -> EngineResult:
    """Scan a repository snapshot and return inventory + candidates."""
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(f"scan root is not a directory: {root}")
    result = EngineResult(root=str(root))
    for rel, is_dir, full in _iter_snapshot(root, scope):
        if is_dir:
            # Only ignored directories are yielded; record the pruning.
            result.inventory.append(FileInventory(
                relpath=rel, size=0, status=STATUS_IGNORED,
                reason="scope-ignore-directory-pruned"))
            continue
        if scope.is_ignored(rel, is_dir=False):
            result.inventory.append(FileInventory(
                relpath=rel, size=0, status=STATUS_IGNORED, reason="scope-ignore"))
            continue
        try:
            lst = os.lstat(full)
        except OSError as exc:
            result.inventory.append(FileInventory(
                relpath=rel, size=0, status=STATUS_UNREADABLE,
                reason=f"stat-failed:{exc.__class__.__name__}"))
            continue
        if os.path.islink(full):
            # Symlinks are not followed: they could point outside the snapshot.
            result.inventory.append(FileInventory(
                relpath=rel, size=lst.st_size, status=STATUS_SYMLINK,
                reason="symlinks-are-not-followed"))
            continue
        size = lst.st_size
        if size >= scope.max_file_bytes:
            result.inventory.append(FileInventory(
                relpath=rel, size=size, status=STATUS_OVERSIZE,
                reason=f"size>={scope.max_file_bytes}-bytes-not-opened"))
            continue
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            result.inventory.append(FileInventory(
                relpath=rel, size=size, status=STATUS_UNREADABLE,
                reason=f"read-failed:{exc.__class__.__name__}"))
            continue
        candidates, kind_name, kind_reason = scan_file(
            rel, data, rules, scope, fingerprinter)
        result.candidates.extend(candidates)
        result.inventory.append(FileInventory(
            relpath=rel, size=len(data), status=STATUS_SCANNED,
            media=kind_name, reason=kind_reason,
            sha256=file_sha256(data)))
    return result
