"""Failure classification.

Every unsuccessful outcome has an explicit :class:`RejectionCategory`.  The
service never collapses exceptions or unknown states into a success result:
unknown reader errors are classified as ``ARCHIVE_CORRUPT`` (or
``INTERNAL_ERROR`` for programmer errors) and the run is marked rejected.
"""

from __future__ import annotations

from enum import Enum


class RejectionCategory(str, Enum):
    """Stable failure codes used in API responses, audit rows and tests."""

    FORMAT_UNSUPPORTED = "FORMAT_UNSUPPORTED"
    """Not a recognized ZIP/TAR archive."""

    FORMAT_AMBIGUOUS = "FORMAT_AMBIGUOUS"
    """Matched more than one format signature; rejected to avoid parser confusion."""

    ARCHIVE_CORRUPT = "ARCHIVE_CORRUPT"
    """Malformed central directory, truncated stream, undecodable name, ..."""

    ENTRY_SPECIAL = "ENTRY_SPECIAL"
    """FIFO, device, socket or other non-regular/non-directory/non-symlink entry."""

    HARDLINK = "HARDLINK"
    """Hard link entry (explicitly refused)."""

    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    """Absolute path, drive letter or ``..`` component in the entry name."""

    PATH_INVALID = "PATH_INVALID"
    """NUL byte, backslash or otherwise structurally invalid name."""

    CASE_COLLISION = "CASE_COLLISION"
    """Two entries differ only by case (case-insensitive filesystem collision)."""

    DUPLICATE_ENTRY = "DUPLICATE_ENTRY"
    """The exact same path is declared more than once."""

    PATH_CONFLICT = "PATH_CONFLICT"
    """A file and a directory claim the same path, or a non-symlink file sits
    under a path another entry declares as a file."""

    SYMLINK_ESCAPE = "SYMLINK_ESCAPE"
    """Symlink target is absolute, escapes the root, contains invalid bytes or
    traverses a non-directory."""

    SYMLINK_LOOP = "SYMLINK_LOOP"
    """Symlink resolution exceeds the hop budget (link chain / cycle)."""

    SYMLINK_DANGLING = "SYMLINK_DANGLING"
    """Symlink points to a path that no entry provides."""

    BUDGET_TOTAL_BYTES = "BUDGET_TOTAL_BYTES"
    """Declared uncompressed total exceeds the byte budget (zip bomb)."""

    BUDGET_FILE_COUNT = "BUDGET_FILE_COUNT"
    """Entry count exceeds the file-count budget."""

    BUDGET_DEPTH = "BUDGET_DEPTH"
    """Path nesting exceeds the depth budget."""

    BUDGET_RATIO = "BUDGET_RATIO"
    """Declared uncompressed/compressed ratio exceeds the ratio budget."""

    DECLARED_SIZE_MISMATCH = "DECLARED_SIZE_MISMATCH"
    """The number of payload bytes actually produced differs from the declared
    size (truncated tar, extended stream)."""

    CONTENT_CRC_MISMATCH = "CONTENT_CRC_MISMATCH"
    """ZIP payload checksum does not match the central-directory CRC32."""

    VERIFY_FAILED = "VERIFY_FAILED"
    """Independent post-extraction walk disagrees with the plan."""

    CONTAINMENT_VIOLATION = "CONTAINMENT_VIOLATION"
    """A resolved path was observed outside the isolated run directory."""

    UPLOAD_LIMIT = "UPLOAD_LIMIT"
    """Uploaded archive exceeds the configured request size limit."""

    INTERNAL_ERROR = "INTERNAL_ERROR"
    """Unexpected programmer-level error; surfaced, never hidden as success."""


class RejectionError(Exception):
    """A classified, auditable failure.

    :param category: stable failure category
    :param detail: human-readable explanation (must not contain secrets)
    :param entry: offending entry name as stored in the archive, when applicable
    """

    def __init__(
        self,
        category: RejectionCategory,
        detail: str,
        *,
        entry: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail
        self.entry = entry

    def to_dict(self) -> dict[str, str | None]:
        return {
            "category": self.category.value,
            "detail": self.detail,
            "entry": self.entry,
        }


# HTTP status for each category.  Everything except oversized uploads is 422
# (syntactically accepted request, semantically unsafe/invalid archive).
HTTP_STATUS: dict[RejectionCategory, int] = {
    RejectionCategory.UPLOAD_LIMIT: 413,
    RejectionCategory.INTERNAL_ERROR: 500,
}
