"""Security kernel: turn a snapshot directory into masked candidates.

Design constraints (see project README):
  * raw matched bytes leave the function as mask + fingerprint only;
  * structural rules and the entropy heuristic are distinct signals, and
    entropy-only hits are always marked uncertain;
  * text and binary are scanned with the same byte pipeline, but positions
    are reported as line/column for text and byte offsets for binary;
  * oversize files are never opened; symlinks are not followed by default;
  * the scanner holds no baseline state - state classification is the job
    of the state layer, so the core stays independently testable.
"""

from __future__ import annotations

import bisect
import os
from datetime import datetime, timezone
from pathlib import Path

from .entropy import iter_token_spans, shannon_entropy
from .fileclass import BINARY, TEXT, classify_bytes
from .fingerprint import CandidateFingerprinter, file_sha256
from .ignore import IgnorePolicy
from .mask import mask_value
from .models import (
    Candidate,
    FileError,
    RawHit,
    REASON_SYMLINK_SKIPPED,
    REASON_TOO_LARGE,
    REASON_UNREADABLE,
    ScannedFile,
    ScanResult,
    SkippedFile,
    Location,
    STATE_NEW,
    TRIAGE_UNTRIPPED,
)
from .pathpolicy import rel_posix, resolve_root
from .rules import RuleSet


def _line_mapping(data: bytes):
    """Return (line_starts, line_of) helpers over LF-separated bytes."""
    starts = [0]
    for i, b in enumerate(data):
        if b == 0x0A:  # '\n'
            starts.append(i + 1)
    starts.append(len(data) + 1)

    def line_col(offset: int) -> tuple[int, int]:
        idx = bisect.bisect_right(starts, offset) - 1
        line_start = starts[idx]
        return idx + 1, offset - line_start + 1

    return starts, line_col


class Scanner:
    def __init__(self, ruleset: RuleSet, fingerprinter: CandidateFingerprinter):
        self._rs = ruleset
        self._fp = fingerprinter
        self._ignore = IgnorePolicy(ruleset.ignore_patterns)

    # ------------------------------------------------------------------ walk
    def _walk(self, root: Path):
        """Yield (rel_posix, absolute_path, is_dir) in sorted order."""
        stack: list[tuple[str, Path]] = [("", root)]
        while stack:
            rel_dir, directory = stack.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda e: e.name)
            except OSError as exc:
                if rel_dir:
                    yield rel_dir, directory, False, exc
                continue
            dirs = []
            for entry in entries:
                rel = entry.name if not rel_dir else rel_dir + "/" + entry.name
                is_link = entry.is_symlink()
                if is_link and not self._rs.scope.follow_symlinks:
                    yield rel, Path(entry.path), False, REASON_SYMLINK_SKIPPED
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError as exc:
                    yield rel, Path(entry.path), False, exc
                    continue
                if is_dir:
                    if self._ignore.is_dir_ignored(rel):
                        yield rel, Path(entry.path), True, None  # ignored marker
                    else:
                        dirs.append((rel, Path(entry.path)))
                else:
                    if self._ignore.is_file_ignored(rel):
                        yield rel, Path(entry.path), False, "ignored"
                    else:
                        yield rel, Path(entry.path), False, None
            # push dirs reversed so sorted pop order stays deterministic
            for item in reversed(dirs):
                stack.append(item)

    # ------------------------------------------------------------- per file
    def _scan_content(self, rel: str, data: bytes) -> list[RawHit]:
        kind = classify_bytes(data)
        _, line_col = _line_mapping(data)
        hits: list[RawHit] = []
        claimed_spans: list[tuple[int, int]] = []

        def loc_for(start: int, end: int) -> Location:
            if kind == TEXT:
                line, column = line_col(start)
            else:
                line, column = 1, start + 1
            return Location(
                path=rel,
                line=line,
                column=column,
                byte_offset=start,
                byte_end=end,
                kind=kind,
            )

        def claimed(start: int, end: int) -> bool:
            for s, e in claimed_spans:
                if start < e and s < end:
                    return True
            return False

        # 1) Structural rules, in configured priority order.
        for rule in self._rs.rules:
            for m in rule.compiled.finditer(data):
                try:
                    secret = m.group("secret")
                except IndexError:
                    continue
                start, end = m.span("secret")
                if claimed(start, end):
                    continue
                entropy = shannon_entropy(secret)
                reasons: list[str] = []
                uncertain = False
                if rule.min_entropy is not None and entropy < rule.min_entropy:
                    uncertain = True
                    reasons.append(
                        f"below_rule_entropy_floor:{rule.min_entropy}"
                    )
                claimed_spans.append((start, end))
                hits.append(
                    RawHit(
                        rule_id=rule.id,
                        masked=mask_value(secret),
                        fingerprint=self._fp.fingerprint(secret),
                        entropy=entropy,
                        confidence=rule.confidence,
                        category=rule.category,
                        source="rule",
                        uncertain=uncertain,
                        reasons=reasons,
                        locations=[loc_for(start, end)],
                    )
                )

        # 2) Entropy-only heuristic over ASCII tokens, skipping claimed spans.
        min_len = self._rs.entropy.min_token_length
        threshold = self._rs.entropy.shannon_threshold
        for start, end, token in iter_token_spans(data, min_len):
            if claimed(start, end):
                continue
            entropy = shannon_entropy(token)
            if entropy < threshold:
                continue
            hits.append(
                RawHit(
                    rule_id="entropy_heuristic",
                    masked=mask_value(token),
                    fingerprint=self._fp.fingerprint(token),
                    entropy=entropy,
                    confidence="low",
                    category="uncertain_high_entropy",
                    source="entropy",
                    uncertain=True,
                    reasons=[f"entropy_only:>={threshold}"],
                    locations=[loc_for(start, end)],
                )
            )
        return hits

    # ------------------------------------------------------------------ run
    def scan(
        self,
        root: str | Path,
        *,
        scan_id: str,
        project_id: str,
        request_id: str,
        started_at: datetime | None = None,
    ) -> ScanResult:
        root_path = resolve_root(root)
        started_at = started_at or datetime.now(timezone.utc)
        result = ScanResult(
            scan_id=scan_id,
            project_id=project_id,
            root=str(root_path),
            rules_version=self._rs.rules_version,
            classification_version=self._rs.classification_version,
            config_digest=self._rs.raw_digest,
            started_at=started_at.isoformat(),
            finished_at="",
            request_id=request_id,
        )
        aggregated: dict[tuple[str, str], RawHit] = {}
        order: list[tuple[str, str]] = []

        for item in self._walk(root_path):
            rel, abspath, is_dir, marker = item
            if is_dir:
                # ignored directory marker; record for the manifest
                result.ignored.append(rel + "/")
                continue
            if marker == "ignored":
                result.ignored.append(rel)
                continue
            if marker == REASON_SYMLINK_SKIPPED:
                result.skipped.append(
                    SkippedFile(
                        path=rel, kind="", reason=REASON_SYMLINK_SKIPPED,
                        detail="symlink not followed by configuration",
                    )
                )
                continue
            if isinstance(marker, OSError):
                result.errors.append(
                    FileError(path=rel, code=REASON_UNREADABLE, message="directory listing failed")
                )
                continue

            try:
                st = abspath.stat()
            except OSError:
                result.errors.append(
                    FileError(path=rel, code=REASON_UNREADABLE, message="stat failed")
                )
                continue
            if st.st_size > self._rs.scope.max_file_bytes:
                result.skipped.append(
                    SkippedFile(
                        path=rel,
                        kind="",
                        reason=REASON_TOO_LARGE,
                        detail=(
                            f"size {st.st_size} > limit "
                            f"{self._rs.scope.max_file_bytes}; not opened"
                        ),
                        size_bytes=st.st_size,
                    )
                )
                continue
            try:
                fd = os.open(abspath, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    data = os.read(fd, st.st_size)
                finally:
                    os.close(fd)
            except OSError:
                result.errors.append(
                    FileError(path=rel, code=REASON_UNREADABLE, message="file could not be read")
                )
                continue

            kind = classify_bytes(data)
            hits = self._scan_content(rel, data)
            result.files.append(
                ScannedFile(
                    path=rel,
                    kind=kind,
                    size_bytes=len(data),
                    sha256=file_sha256(data),
                    candidate_hits=len(hits),
                )
            )
            for hit in hits:
                key = (hit.rule_id, hit.fingerprint)
                existing = aggregated.get(key)
                if existing is None:
                    aggregated[key] = hit
                    order.append(key)
                else:
                    existing.locations.extend(hit.locations)

        for key in order:
            hit = aggregated[key]
            files = sorted({loc.path for loc in hit.locations})
            result.candidates.append(
                Candidate(
                    rule_id=hit.rule_id,
                    category=hit.category,
                    confidence=hit.confidence,
                    masked=hit.masked,
                    fingerprint=hit.fingerprint,
                    entropy=hit.entropy,
                    source=hit.source,
                    uncertain=hit.uncertain,
                    reasons=list(hit.reasons),
                    first_seen_scan_id=scan_id,
                    state=STATE_NEW,
                    triage=TRIAGE_UNTRIPPED,
                    locations=list(hit.locations),
                    files=files,
                )
            )

        result.finished_at = datetime.now(timezone.utc).isoformat()
        return result
