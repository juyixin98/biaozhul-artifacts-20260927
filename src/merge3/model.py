"""Core value types shared by the diff, merge, storage and API layers."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class EditKind(str, enum.Enum):
    INSERT = "insert"  # start == end, replacement != ""
    DELETE = "delete"  # start <  end, replacement == ""
    REPLACE = "replace"  # start <  end, replacement != ""


@dataclass(frozen=True)
class Edit:
    """A range edit against one document version (usually the baseline).

    Offsets are character offsets into that version; the half-open range
    ``[start, end)`` is replaced by ``replacement``.  ``side`` records which
    participant produced the edit (``"local"`` / ``"remote"``) and ``kind``
    is derived.  ``edit_id`` is stable per generated edit set and is used in
    diagnostics so a decision can be traced back to the exact source ranges.
    """

    start: int
    end: int
    replacement: str
    side: str
    edit_id: str

    @property
    def kind(self) -> EditKind:
        if self.start == self.end:
            if self.replacement == "":
                raise ValueError("empty edit: zero span and zero replacement")
            return EditKind.INSERT
        if self.replacement == "":
            return EditKind.DELETE
        return EditKind.REPLACE

    @property
    def is_point(self) -> bool:
        return self.start == self.end

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < 0 or self.start > self.end:
            raise ValueError(f"bad range [{self.start}, {self.end})")
        # Force validation through the kind property.
        _ = self.kind


@dataclass(frozen=True)
class Region:
    """A half-open range inside one named document."""

    document: str  # "base" | "local" | "remote" | "merged"
    start: int
    end: int
    line_start: int
    line_end: int

    def slice(self, text: str) -> str:
        return text[self.start : self.end]


class ConflictType(str, enum.Enum):
    #: both sides inserted at the same point with different content
    SAME_POINT_INSERT = "same_point_insert"
    #: one side deletes what the other changes
    DELETE_MODIFY = "delete_modify"
    #: both sides change the same span differently
    DIVERGENT_MODIFY = "divergent_modify"
    #: the two edit ranges partially overlap (neither disjoint nor equal)
    PARTIAL_OVERLAP = "partial_overlap"
    #: one side inserts at a boundary strictly inside the other's changed span
    INSERT_RANGE = "insert_range"


@dataclass(frozen=True)
class ConflictBlock:
    """One unresolved region, carrying the full three-way provenance.

    ``base_region`` / ``local_region`` / ``remote_region`` point into the
    three input documents and are exact character/line ranges.  ``local_text``
    and ``remote_text`` are the materialized alternatives; for a point insert
    the opposing side's region is the same empty point and its text is ``""``.
    ``allowed_resolutions`` names every explicit choice the caller may make;
    the core never guesses among them.
    """

    conflict_id: str
    conflict_type: ConflictType
    base_region: Region
    local_region: Region
    remote_region: Region
    base_text: str
    local_text: str
    remote_text: str
    local_edit_ids: tuple[str, ...]
    remote_edit_ids: tuple[str, ...]
    allowed_resolutions: tuple[str, ...]

    def to_dict(self) -> dict:
        def region(r: Region) -> dict:
            return {
                "document": r.document,
                "start": r.start,
                "end": r.end,
                "line_start": r.line_start,
                "line_end": r.line_end,
                "text": None,  # text is attached at block level, never duplicated
            }

        return {
            "conflict_id": self.conflict_id,
            "conflict_type": self.conflict_type.value,
            "base_region": region(self.base_region),
            "local_region": region(self.local_region),
            "remote_region": region(self.remote_region),
            "base_text": self.base_text,
            "local_text": self.local_text,
            "remote_text": self.remote_text,
            "local_edit_ids": list(self.local_edit_ids),
            "remote_edit_ids": list(self.remote_edit_ids),
            "allowed_resolutions": list(self.allowed_resolutions),
        }


@dataclass
class MergeResult:
    """Outcome of a three-way merge.

    * If ``conflicts`` is empty, ``merged_text`` is the automatic merge and
      ``auto_merged`` is true.
    * Otherwise ``merged_text`` is ``None``: the core never emits conflict
      markers or guessed content.  The caller resolves every conflict by id
      and calls :func:`merge3.merge.rebuild_merged_text`.
    """

    merged_text: Optional[str]
    conflicts: list[ConflictBlock] = field(default_factory=list)
    local_edits: list[Edit] = field(default_factory=list)
    remote_edits: list[Edit] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    auto_merged: bool = False
    request_id: str = ""

    def summary(self) -> dict:
        return {
            "request_id": self.request_id,
            "auto_merged": self.auto_merged,
            "conflict_count": len(self.conflicts),
            "local_edit_count": len(self.local_edits),
            "remote_edit_count": len(self.remote_edits),
        }
