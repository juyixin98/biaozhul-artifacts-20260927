"""Serialize cues back into SRT / WebVTT text.

Text payloads are emitted byte-for-byte as parsed (markup and encoding
preserved); only timing lines are regenerated. No cue is ever omitted: the
caller passes exactly one output row per parsed cue.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.timeutil import format_ms

_BOM = "﻿"


@dataclass(frozen=True)
class RenderedCue:
    start_ms: int
    end_ms: int
    raw_lines: tuple[str, ...]
    identifier: str | None = None


def render_document(
    fmt: str,
    rows: list[RenderedCue],
    *,
    had_bom: bool = False,
    header: str = "",
) -> str:
    if fmt == "vtt":
        return _render_vtt(rows, header=header or "WEBVTT")
    return _render_srt(rows, had_bom=had_bom)


def _render_srt(rows: list[RenderedCue], *, had_bom: bool) -> str:
    parts: list[str] = []
    if had_bom:
        parts.append(_BOM)
    for n, row in enumerate(rows, start=1):
        timing = f"{format_ms(row.start_ms, 'srt')} --> {format_ms(row.end_ms, 'srt')}"
        block = "\n".join([str(n), timing, *row.raw_lines])
        parts.append(block)
    # SRT files end with a trailing blank line after the last block.
    return "\n\n".join(parts) + "\n\n"


def _render_vtt(rows: list[RenderedCue], *, header: str) -> str:
    parts = [header, ""]
    for row in rows:
        timing = f"{format_ms(row.start_ms, 'vtt')} --> {format_ms(row.end_ms, 'vtt')}"
        block_lines: list[str] = []
        if row.identifier is not None:
            block_lines.append(row.identifier)
        block_lines.append(timing)
        block_lines.extend(row.raw_lines)
        parts.append("\n".join(block_lines))
    return "\n\n".join(parts) + "\n"
