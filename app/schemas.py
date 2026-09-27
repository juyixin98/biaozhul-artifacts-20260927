"""Pydantic request models."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .r128_constants import ROLE_WEIGHTS

SampleFormat = Literal["s16", "s24", "s32", "f32", "f64"]
_KNOWN_ROLES = sorted(set(ROLE_WEIGHTS) | {"LFE"})


class JobCreateRequest(BaseModel):
    channels: int = Field(..., ge=1, le=6,
                          description="number of interleaved channels (1/2/6)")
    sample_format: SampleFormat
    roles: list[str] | None = Field(
        None,
        description="optional explicit per-channel roles; defaults to mono/"
                    "stereo/5.1 ITU layouts. Known: " + ", ".join(_KNOWN_ROLES),
    )

    @field_validator("roles")
    @classmethod
    def _roles_known(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            bad = [r for r in v if r not in _KNOWN_ROLES]
            if bad:
                raise ValueError(f"unknown roles {bad}; known: {_KNOWN_ROLES}")
        return v
