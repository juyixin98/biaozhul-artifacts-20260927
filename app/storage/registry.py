"""Version registry: publishes versions and hands out pinned snapshots.

In-flight pinning semantics
---------------------------
A request resolves its version **once, at the start** and receives an immutable
:class:`~app.storage.snapshot.Snapshot`.  Any publish that happens while the
request is still running neither changes nor invalidates that snapshot: the new
version only affects later requests that did not already pin one.  Snapshots
are cached in memory; older versions remain available for pinning.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from ..core.segmenter import SegmentationResult, segment_text
from ..diagnostics import ACCEPT, REJECT, RequestDiagnostics
from .models import PreparedEntry, validate_entries
from .repository import DictionaryRepository, VersionInfo
from .snapshot import Snapshot, build_snapshot


class VersionNotFoundError(KeyError):
    """Raised when a pinned version ref does not exist."""


class VersionRegistry:
    def __init__(self, repository: DictionaryRepository, *, min_word_cost: float = 0.01,
                 unknown_char_cost: float = 8.0, max_word_length: int = 32) -> None:
        self._repo = repository
        self._min_word_cost = min_word_cost
        self._unknown_char_cost = unknown_char_cost
        self._max_word_length = max_word_length
        self._cache: dict[str, Snapshot] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- publishing
    def prepare(self, payload: Any) -> list[PreparedEntry]:
        """Validate a raw payload (raises PublishValidationError)."""
        return validate_entries(payload)

    def publish_prepared(self, entries: list[PreparedEntry], note: Optional[str] = None) -> Snapshot:
        version = self._repo.publish(entries, note=note)
        return self._load(version)

    def publish(self, payload: Any, note: Optional[str] = None) -> Snapshot:
        entries = self.prepare(payload)
        return self.publish_prepared(entries, note=note)

    # ---------------------------------------------------------------- lookup
    def _load(self, version: str) -> Snapshot:
        with self._lock:
            cached = self._cache.get(version)
            if cached is not None:
                return cached
        infos = {v.version: v for v in self._repo.list_versions()}
        if version not in infos:
            raise VersionNotFoundError(version)
        stored, _total = self._repo.load_entries(version)
        snapshot = build_snapshot(infos[version], stored, self._min_word_cost)
        with self._lock:
            self._cache.setdefault(version, snapshot)
        return snapshot

    def resolve(self, ref: Optional[str]) -> Snapshot:
        """Resolve 'current'/None or a pinned version to an immutable snapshot."""
        version = self._repo.resolve_version(ref)
        if version is None:
            raise VersionNotFoundError(ref or "current")
        return self._load(version)

    def current(self) -> Optional[Snapshot]:
        version = self._repo.current_version()
        return self._load(version) if version else None

    def list_versions(self) -> list[VersionInfo]:
        return self._repo.list_versions()

    def ensure_seeded(self, entries: list[dict[str, Any]], note: str = "synthetic seed") -> Optional[Snapshot]:
        """Publish the seed dictionary only when no version exists yet."""
        if self._repo.has_any_version():
            return None
        return self.publish(entries, note=note)

    # ------------------------------------------------------------- operation
    def segment(self, text: str, diag: RequestDiagnostics,
                version_ref: Optional[str] = None) -> SegmentationResult:
        """Resolve+pin the snapshot for this request, then segment."""
        pinned_ref = version_ref
        try:
            snapshot = self.resolve(pinned_ref)
        except VersionNotFoundError:
            diag.add(
                "version_resolution",
                REJECT,
                "pinned dictionary version does not exist; cannot segment",
                requested=pinned_ref,
            )
            raise
        diag.add(
            "version_resolution",
            ACCEPT,
            "pinned immutable snapshot for the whole request"
            if pinned_ref not in (None, "", "current")
            else "no pin supplied; resolved current version at request start",
            requested=pinned_ref or "current",
            pinned=snapshot.version,
            word_count=snapshot.word_count(),
        )
        fp = diag.text_fingerprint(text)
        result, norm = segment_text(
            text,
            snapshot.trie,
            version=snapshot.version,
            request_id=diag.request_id,
            unknown_char_cost=self._unknown_char_cost,
            max_word_length=self._max_word_length,
        )
        diag.add(
            "normalization",
            ACCEPT if result.orig_covered else REJECT,
            "original offsets tile the input exactly" if result.orig_covered
            else "offset mapping failed to cover the input",
            input=fp,
            source_length=len(text),
            normalized_length=norm.length,
            removed_chars=result.removed_chars,
        )
        diag.add(
            "dag_search",
            ACCEPT,
            "optimal and runner-up paths computed over the DAG",
            tokens=result.best.token_count,
            unknown_tokens=result.unknown_tokens,
            best_cost=round(result.best.cost, 6),
            gap=result.gap_rounded,
            gap_status=result.gap_status,
            coverage_complete=result.orig_covered,
            reconstruction_ok=result.reconstructed,
        )
        return result
