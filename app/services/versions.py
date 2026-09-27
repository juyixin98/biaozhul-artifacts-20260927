"""Pattern-version service.

Owns the lifecycle "raw base64 patterns -> validated bytes -> TextSpec ->
compiled immutable Automaton -> persisted version metadata", plus a small
process-local cache of compiled automata so opening a scan never recompiles.
"""
from __future__ import annotations

import uuid
from typing import Dict, List, Tuple

from ..automaton import Automaton, AutomatonBuildError
from ..errors import (
    DomainError,
    DuplicatePatternError,
    EmptyPatternError,
    UnsupportedEncodingError,
    VersionNotFoundError,
)
from ..spec import CaseMode, SUPPORTED_ENCODINGS, TextSpec, encode_base64
from ..storage.version_repo import VersionRepo


class VersionService:
    def __init__(self, repo: VersionRepo, *, max_patterns: int,
                 max_pattern_bytes: int):
        self._repo = repo
        self._max_patterns = max_patterns
        self._max_pattern_bytes = max_pattern_bytes
        self._cache: Dict[str, Tuple[Automaton, TextSpec]] = {}

    # ---- creation -----------------------------------------------------------

    def create_version(
        self,
        patterns_b64: List[str],
        *,
        encoding: str,
        case_mode: str,
        name: str = "",
    ) -> Tuple[str, TextSpec, Automaton]:
        # Validate spec parameters explicitly so callers get the typed
        # "unsupported" rejection rather than an enum ValueError.
        if encoding not in SUPPORTED_ENCODINGS:
            raise UnsupportedEncodingError(
                f"encoding {encoding!r} is not supported",
                details={"supported": list(SUPPORTED_ENCODINGS)},
            )
        try:
            cm = CaseMode(case_mode)
        except ValueError as exc:
            raise UnsupportedEncodingError(
                f"unknown case_mode {case_mode!r}"
            ) from exc
        spec = TextSpec(encoding=encoding, case_mode=cm)

        if not patterns_b64:
            raise EmptyPatternError(
                "patterns must be a non-empty list",
                details={"index": -1},
            )
        if len(patterns_b64) > self._max_patterns:
            raise EmptyPatternError(
                f"too many patterns: {len(patterns_b64)} > "
                f"{self._max_patterns}",
                code="too_many_patterns",
                http_status=422,
            )

        normalized: List[bytes] = []
        seen: Dict[bytes, int] = {}
        for idx, b64 in enumerate(patterns_b64):
            # decode_base64 raises InvalidBase64Error (typed 422).
            from ..spec import decode_base64
            raw = decode_base64(b64, what=f"patterns[{idx}]")
            if len(raw) == 0:
                raise EmptyPatternError(
                    f"patterns[{idx}] is empty after base64 decoding",
                    details={"index": idx},
                )
            if len(raw) > self._max_pattern_bytes:
                raise EmptyPatternError(
                    f"patterns[{idx}] is {len(raw)} bytes, limit "
                    f"{self._max_pattern_bytes}",
                    details={"index": idx, "length": len(raw),
                             "limit": self._max_pattern_bytes},
                    code="pattern_too_long",
                    http_status=422,
                )
            try:
                norm = spec.normalize_pattern(raw)
            except ValueError as exc:
                raise EmptyPatternError(
                    f"patterns[{idx}] is empty", details={"index": idx}
                ) from exc
            if norm in seen:
                raise DuplicatePatternError(
                    f"patterns[{idx}] normalizes to the same bytes as "
                    f"patterns[{seen[norm]}] under spec "
                    f"{encoding}/{case_mode}; duplicate patterns are rejected",
                    details={"index": idx,
                             "duplicate_of": seen[norm],
                             "length": len(norm)},
                )
            seen[norm] = idx
            normalized.append(norm)

        try:
            automaton = Automaton(normalized)
        except AutomatonBuildError as exc:
            # Defensive: normalization should have caught every case.
            raise EmptyPatternError(str(exc), code="automaton_build_failed",
                                    http_status=422) from exc

        version_id = uuid.uuid4().hex
        self._repo.insert_version(
            version_id=version_id,
            name=name or version_id,
            encoding=encoding,
            case_mode=case_mode,
            pattern_b64=[encode_base64(p) for p in normalized],
            node_count=automaton.node_count,
        )
        self._cache[version_id] = (automaton, spec)
        return version_id, spec, automaton

    # ---- reads --------------------------------------------------------------

    def load(self, version_id: str) -> Tuple[Automaton, TextSpec]:
        """Return the compiled automaton + spec for a version.

        Compilation is deterministic from the persisted normalized patterns,
        so a cache miss (e.g. after process restart) is transparent.
        """
        cached = self._cache.get(version_id)
        if cached is not None:
            return cached
        row = self._repo.get(version_id)
        if row is None:
            raise VersionNotFoundError(
                f"version {version_id} does not exist",
                details={"version_id": version_id},
            )
        from ..spec import decode_base64
        patterns = [decode_base64(b, what="stored pattern")
                    for b in self._repo.get_patterns(version_id)]
        automaton = Automaton(patterns)
        spec = TextSpec(
            encoding=row["encoding"],
            case_mode=CaseMode(row["case_mode"]),
        )
        self._cache[version_id] = (automaton, spec)
        return automaton, spec

    def describe(self, version_id: str) -> dict:
        row = self._repo.get(version_id)
        if row is None:
            raise VersionNotFoundError(
                f"version {version_id} does not exist",
                details={"version_id": version_id},
            )
        return dict(row)

    def list_versions(self, limit: int = 100) -> List[dict]:
        return [dict(r) for r in self._repo.list_versions(limit)]
