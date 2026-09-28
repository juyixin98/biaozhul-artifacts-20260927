"""Plan data model.

A :class:`Plan` is an *immutable, source-bound* description of what applying a
ruleset would do.  Every edit carries raw byte offsets into the exact source
buffer identified by ``source_sha256``, plus the already-rendered replacement
bytes (capture references are expanded at build time, after validation, so a
stored plan can be replayed without re-running the engine).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Edit:
    """One replacement: source[start:end] -> ``replacement`` (may be empty)."""

    start: int
    end: int
    replacement: bytes
    rule_id: str
    matched: bytes
    """The exact original bytes covered; kept for diagnostics and replay."""
    zero_width: bool

    def to_jsonable(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "zero_width": self.zero_width,
            "rule_id": self.rule_id,
            "matched": self.matched.decode("utf-8"),
            "replacement": self.replacement.decode("utf-8"),
        }


@dataclass(frozen=True, slots=True)
class Plan:
    source_sha256: str
    source_length: int
    normalize_newlines: bool
    edits: tuple[Edit, ...]
    rule_ids: tuple[str, ...]
    engine: str = "re2"

    @property
    def edit_count(self) -> int:
        return len(self.edits)

    def is_bound_to(self, source_sha256: str) -> bool:
        return self.source_sha256 == source_sha256

    def to_json(self) -> str:
        return json.dumps(
            {
                "engine": self.engine,
                "source_sha256": self.source_sha256,
                "source_length": self.source_length,
                "normalize_newlines": self.normalize_newlines,
                "rule_ids": list(self.rule_ids),
                "edits": [e.to_jsonable() for e in self.edits],
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, text: str | bytes) -> "Plan":
        d = json.loads(text)
        edits = tuple(
            Edit(
                start=int(e["start"]),
                end=int(e["end"]),
                replacement=e["replacement"].encode("utf-8"),
                rule_id=e["rule_id"],
                matched=e["matched"].encode("utf-8"),
                zero_width=bool(e.get("zero_width")),
            )
            for e in d["edits"]
        )
        return cls(
            source_sha256=d["source_sha256"],
            source_length=int(d["source_length"]),
            normalize_newlines=bool(d.get("normalize_newlines", False)),
            edits=edits,
            rule_ids=tuple(d.get("rule_ids", ())),
            engine=d.get("engine", "re2"),
        )


@dataclass(slots=True)
class Decision:
    """One overlap-resolution verdict, for diagnostics / replays."""

    stage: str  # "candidate" | "accept" | "reject"
    rule_id: str
    start: int
    end: int
    reason: str
    zero_width: bool = False
    conflicts_with: str | None = None

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "rule_id": self.rule_id,
            "start": self.start,
            "end": self.end,
            "zero_width": self.zero_width,
            "reason": self.reason,
            "conflicts_with": self.conflicts_with,
        }


@dataclass(slots=True)
class PlanResult:
    plan: Plan
    decisions: list[Decision] = field(default_factory=list)
    candidates_total: int = 0
    candidates_dropped: int = 0

    def trace_json(self) -> str:
        return json.dumps(
            {
                "source_sha256": self.plan.source_sha256,
                "candidates_total": self.candidates_total,
                "candidates_dropped": self.candidates_dropped,
                "edits": self.plan.edit_count,
                "decisions": [d.as_dict() for d in self.decisions],
            },
            ensure_ascii=False,
        )
