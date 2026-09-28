"""HTTP 请求/响应 Pydantic 模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EntryIn(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    surface: str = Field(min_length=1, max_length=512)
    score: int = Field(ge=0)


class BatchIn(BaseModel):
    items: list[EntryIn] = Field(min_length=1, max_length=10000)


class ScoreIn(BaseModel):
    score: int = Field(ge=0)


class AdjustIn(BaseModel):
    delta: int


class SnapshotIn(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"[A-Za-z0-9-_.]+")
    note: str = Field(default="", max_length=512)


class EntryOut(BaseModel):
    id: str
    surface: str
    normalized_key: str
    score: int

    @classmethod
    def from_entry(cls, e: Any) -> "EntryOut":
        return cls(id=e.id, surface=e.surface, normalized_key=e.key, score=e.score)


class PruneOut(BaseModel):
    subtree_prefix: str
    upper_bound: int
    best_k_score: int
    justification: str


class EventOut(BaseModel):
    step: int
    event: str
    node_seq: int | None
    edge_seq: int | None
    edge_label: str | None
    prefix: str
    upper_bound: int | None
    detail: str


class TraceOut(BaseModel):
    location: str
    stats: dict[str, int]
    prunes: list[PruneOut]
    events: list[EventOut]


class CompletionOut(BaseModel):
    ok: bool = True
    prefix: str
    normalized_prefix: str
    k: int
    count: int
    entries: list[EntryOut]
    trace: TraceOut | None = None


class SnapshotOut(BaseModel):
    ok: bool = True
    snapshot: dict[str, Any]
