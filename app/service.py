"""Service layer: orchestrates version pinning, segmentation and diagnostics."""
from __future__ import annotations

from . import diagnostics as diag
from .algorithm import segment
from .config import Settings
from .lexicon import LexiconVersion
from .store import VersionNotFoundError, VersionStore


class ServiceError(Exception):
    """Domain error carrying an HTTP status and a machine-readable reason."""

    def __init__(self, status_code: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.message = message


class SegService:
    def __init__(self, store: VersionStore, settings: Settings, recorder: diag.DiagnosticRecorder) -> None:
        self.store = store
        self.settings = settings
        self.recorder = recorder

    def _resolve_version(self, pinned: int | None) -> tuple[LexiconVersion, bool]:
        """Return (lexicon, was_pinned); raise ServiceError on any bad pin."""
        if pinned is not None:
            try:
                return self.store.load(pinned), True
            except VersionNotFoundError:
                raise ServiceError(
                    404,
                    diag.REASON_VERSION_NOT_FOUND,
                    f"pinned version_id={pinned} does not exist",
                )
        latest = self.store.load_latest()
        if latest is None:
            raise ServiceError(
                409, diag.REASON_EMPTY_LEXICON, "no lexicon version has been published yet"
            )
        return latest, False

    def validate_input(self, text: str, request_id: str, pinned: int | None) -> None:
        if text == "":
            d = diag.make_error_diagnostic(
                request_id=request_id, outcome=diag.REJECTED,
                reason=diag.REASON_EMPTY_TEXT, raw_text=text, pinned=pinned,
                detail="text must contain at least one character",
            )
            self.recorder.record(d)
            raise ServiceError(400, diag.REASON_EMPTY_TEXT, "text must be non-empty")
        if len(text) > self.settings.max_input_chars:
            d = diag.make_error_diagnostic(
                request_id=request_id, outcome=diag.REJECTED,
                reason=diag.REASON_TEXT_TOO_LONG, raw_text=text, pinned=pinned,
                detail=f"limit={self.settings.max_input_chars}",
            )
            self.recorder.record(d)
            raise ServiceError(
                413,
                diag.REASON_TEXT_TOO_LONG,
                f"text longer than {self.settings.max_input_chars} characters",
            )

    def segment_text(self, text: str, pinned: int | None, request_id: str):
        self.validate_input(text, request_id, pinned)
        lex, was_pinned = self._resolve_version(pinned)
        result = segment(
            text,
            lex,
            unknown_char_cost=self.settings.unknown_char_cost,
            close_gap_threshold=self.settings.close_gap_threshold,
        )
        self.recorder.record(
            diag.make_segment_diagnostic(
                request_id=request_id, raw_text=text, result=result,
                version_id=lex.version_id, pinned=pinned if was_pinned else None,
            )
        )
        return result, lex.version_id, was_pinned

    def publish(self, words: list[dict], note: str, request_id: str):
        from .lexicon import WordEntry

        entries = [WordEntry(w["word"], w["freq"]) for w in words]
        try:
            built = self.store.publish(entries, note=note)
        except ValueError as exc:
            self.recorder.record(
                diag.make_error_diagnostic(
                    request_id=request_id, outcome=diag.REJECTED,
                    reason=diag.REASON_INVALID_PAYLOAD, input_chars=None,
                    detail=str(exc),
                )
            )
            raise ServiceError(400, diag.REASON_INVALID_PAYLOAD, str(exc)) from exc
        self.recorder.record(
            diag.make_error_diagnostic(
                request_id=request_id, outcome=diag.ACCEPTED,
                reason="PUBLISHED", version_id=built.version_id,
                detail=f"words={built.word_count}",
            )
        )
        return built
