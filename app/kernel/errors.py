"""Error taxonomy.

Every rejection maps to a *specific* failure category — the API never collapses
exceptions or unknown states into a success result. Categories are part of the
audit record and the JSON error response so tests can assert exact failure kinds.
"""
from __future__ import annotations


class ArchiveError(Exception):
    """Base class for all policy / integrity violations."""

    category: str = "unknown_error"
    http_status: int = 400

    def __init__(self, message: str, *, evidence: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.evidence = evidence  # offending entry name / header / detail

    def to_dict(self) -> dict:
        return {
            "error": self.category,
            "message": self.message,
            "evidence": self.evidence,
        }


# --- Input / format level -------------------------------------------------
class UploadTooLarge(ArchiveError):
    category = "upload_too_large"
    http_status = 413


class UnsupportedFormat(ArchiveError):
    category = "unsupported_format"
    http_status = 415


class UnsupportedCompression(ArchiveError):
    category = "unsupported_compression"
    http_status = 415


class UnsupportedEncryption(ArchiveError):
    category = "unsupported_encryption"
    http_status = 415


class CorruptArchive(ArchiveError):
    category = "corrupt_archive"
    http_status = 422


class UnsupportedEntryType(ArchiveError):
    category = "unsupported_entry_type"
    http_status = 422


# --- Policy / structural level (pre-flight) -------------------------------
class PathEscape(ArchiveError):
    category = "path_escape"
    http_status = 422


class CaseCollision(ArchiveError):
    category = "case_collision"
    http_status = 422


class DuplicateName(ArchiveError):
    category = "duplicate_name"
    http_status = 422


class TypeConflict(ArchiveError):
    category = "type_conflict"
    http_status = 422


class SymlinkEscape(ArchiveError):
    category = "symlink_escape"
    http_status = 422


class SymlinkLoop(ArchiveError):
    category = "symlink_loop"
    http_status = 422


class SymlinkAlias(ArchiveError):
    category = "symlink_alias"
    http_status = 422


class HardlinkRejected(ArchiveError):
    category = "hardlink_rejected"
    http_status = 422


class SymlinkRejected(ArchiveError):
    category = "symlink_rejected"
    http_status = 422


class SpecialFileRejected(ArchiveError):
    category = "special_file_rejected"
    http_status = 422


class UnsafeName(ArchiveError):
    category = "unsafe_name"
    http_status = 422


class BudgetTotalSizeExceeded(ArchiveError):
    category = "budget_total_size_exceeded"
    http_status = 422


class BudgetFileSizeExceeded(ArchiveError):
    category = "budget_file_size_exceeded"
    http_status = 422


class BudgetEntryCountExceeded(ArchiveError):
    category = "budget_entry_count_exceeded"
    http_status = 422


class BudgetDepthExceeded(ArchiveError):
    category = "budget_depth_exceeded"
    http_status = 422


class CompressionBomb(ArchiveError):
    category = "compression_bomb"
    http_status = 422


# --- Runtime extraction level (streaming) ---------------------------------
class DeclaredLengthMismatch(ArchiveError):
    """Declared header length differs from the number of bytes actually yielded."""

    category = "declared_length_mismatch"
    http_status = 422


class IntegrityFailure(ArchiveError):
    """CRC / checksum mismatch, or truncated payload detected while reading."""

    category = "integrity_failure"
    http_status = 422
