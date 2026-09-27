"""Media parsing layer — SRT and restricted-subset WebVTT parsers.

Design contract:

* Parsing never raises for malformed *content*: structural problems become
  ``Diagnostic(severity=ERROR, code=...)`` on the returned :class:`ParseResult`.
* Text payload lines are preserved verbatim (UTF-8, inline markup untouched).
  Only CRLF is normalized to LF; a leading UTF-8 BOM is recorded, not dropped.
* Timestamps are parsed strictly by :mod:`app.core.timeutil`.
* A failed parse returns ``ok=False`` with zero/partial cues; the pipeline
  treats that as a fatal ``parse_failed`` result and never fabricates cues.
"""
from __future__ import annotations

from ..core.markup import scan_markup
from ..core.models import (
    FORMAT_SRT,
    FORMAT_VTT,
    Cue,
    Diagnostic,
    ParseResult,
    Severity,
)
from ..core.timeutil import TimeParseError, parse_ms

_BOM = "﻿"


def parse_bytes(data: bytes, fmt: str | None = None, *, max_cues: int = 10_000) -> ParseResult:
    """Decode UTF-8 strictly, then parse. Bad bytes => encoding_error diagnostic."""
    had_bom = data.startswith(b"\xef\xbb\xbf")
    try:
        text = data.decode("utf-8-sig")  # -sig strips BOM; strict by default
    except UnicodeDecodeError as exc:
        detected = _detect_format_bytes(data)
        result = ParseResult(fmt=detected or FORMAT_SRT, had_bom=had_bom)
        result.diagnostics.append(
            Diagnostic(
                code="encoding_error",
                severity=Severity.ERROR,
                message=f"document is not valid UTF-8: {exc}",
                detail={"byte_offset": exc.start, "reason": exc.reason},
            )
        )
        return result
    return parse_text(text, fmt, max_cues=max_cues, had_bom=had_bom)


def parse_text(
    text: str,
    fmt: str | None = None,
    *,
    max_cues: int = 10_000,
    had_bom: bool = False,
) -> ParseResult:
    if fmt is None:
        fmt = _detect_format_text(text)
    if fmt == FORMAT_VTT:
        return _parse_vtt(text, max_cues, had_bom)
    return _parse_srt(text, max_cues, had_bom)


def _detect_format_bytes(data: bytes) -> str | None:
    head = data[:64].lstrip(b"\xef\xbb\xbf")
    if head.startswith(b"WEBVTT"):
        return FORMAT_VTT
    if b"-->" in head:
        return FORMAT_SRT
    return None


def _detect_format_text(text: str) -> str:
    head = text[:64].lstrip(_BOM)
    if head.lstrip().startswith("WEBVTT"):
        return FORMAT_VTT
    return FORMAT_SRT


def _split_blocks(text: str) -> list[tuple[int, list[str]]]:
    """Return (starting_line_number, lines) for each blank-separated block."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[tuple[int, list[str]]] = []
    current: list[str] = []
    start_line = 1
    for lineno, raw_line in enumerate(normalized.split("\n"), start=1):
        if raw_line.strip() == "":
            if current:
                blocks.append((start_line, current))
                current = []
        else:
            if not current:
                start_line = lineno
            current.append(raw_line)
    if current:
        blocks.append((start_line, current))
    return blocks


def _parse_srt(text: str, max_cues: int, had_bom: bool) -> ParseResult:
    result = ParseResult(fmt=FORMAT_SRT, had_bom=had_bom, header=_BOM if had_bom else "")
    for block_start, lines in _split_blocks(text):
        idx = 0
        if lines and _looks_like_index_line(lines[0]):
            idx = 1
        if idx >= len(lines) or "-->" not in lines[idx]:
            result.diagnostics.append(
                Diagnostic(
                    code="missing_timing",
                    severity=Severity.ERROR,
                    message=f"SRT block starting at line {block_start} has no timing line",
                    detail={"line": block_start, "block_head": lines[0][:40]},
                )
            )
            continue
        timing_diag, start_ms, end_ms = _split_timing(
            lines[idx], FORMAT_SRT, block_start + idx
        )
        if timing_diag is not None:
            result.diagnostics.append(timing_diag)
            continue
        payload = lines[idx + 1 :]
        cue = Cue(index=len(result.cues), start_ms=start_ms, end_ms=end_ms,
                  raw_lines=tuple(payload))
        result.cues.append(cue)
        result.diagnostics.extend(scan_markup(cue, FORMAT_SRT))
        if len(result.cues) > max_cues:
            result.diagnostics.append(
                Diagnostic(
                    code="too_many_cues",
                    severity=Severity.ERROR,
                    message=f"cue limit {max_cues} exceeded",
                    detail={"limit": max_cues},
                )
            )
            break
    return result


def _parse_vtt(text: str, max_cues: int, had_bom: bool) -> ParseResult:
    result = ParseResult(fmt=FORMAT_VTT, had_bom=had_bom)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    head_lines = normalized.split("\n")
    first_nonempty = next((ln for ln in head_lines if ln.strip() != ""), "")
    if not first_nonempty.strip().startswith("WEBVTT"):
        result.diagnostics.append(
            Diagnostic(
                code="invalid_header",
                severity=Severity.ERROR,
                message="WebVTT document must start with a 'WEBVTT' header line",
                detail={"found": first_nonempty[:40]},
            )
        )
        return result
    result.header = first_nonempty.strip()

    for block_start, lines in _split_blocks(normalized):
        if block_start == 1 or (had_bom and block_start <= 2):
            # Header block; only consume when this block is the WEBVTT line.
            if lines and lines[0].strip().startswith("WEBVTT"):
                continue
        first = lines[0].strip()
        if first.startswith("NOTE"):
            # NOTE blocks may start with "NOTE" or "NOTE text..." — both skipped.
            continue
        if first.startswith(("STYLE", "REGION")):
            result.diagnostics.append(
                Diagnostic(
                    code="vtt_block_not_supported",
                    severity=Severity.ERROR,
                    message=f"WebVTT '{first.split()[0]}' blocks are outside the subset "
                    f"(block at line {block_start})",
                    detail={"line": block_start, "kind": first.split()[0]},
                )
            )
            continue

        idx = 0
        identifier: str | None = None
        if "-->" not in lines[0]:
            identifier = lines[0].strip()
            idx = 1
            if idx >= len(lines) or "-->" not in lines[idx]:
                result.diagnostics.append(
                    Diagnostic(
                        code="missing_timing",
                        severity=Severity.ERROR,
                        message=f"WebVTT block at line {block_start}: cue id "
                        f"{identifier!r} not followed by a timing line",
                        detail={"line": block_start},
                    )
                )
                continue
        timing_diag, start_ms, end_ms = _split_timing(
            lines[idx], FORMAT_VTT, block_start + idx
        )
        if timing_diag is not None:
            result.diagnostics.append(timing_diag)
            continue
        payload = lines[idx + 1 :]
        cue = Cue(index=len(result.cues), start_ms=start_ms, end_ms=end_ms,
                  raw_lines=tuple(payload), identifier=identifier)
        result.cues.append(cue)
        result.diagnostics.extend(scan_markup(cue, FORMAT_VTT))
        if len(result.cues) > max_cues:
            result.diagnostics.append(
                Diagnostic(
                    code="too_many_cues",
                    severity=Severity.ERROR,
                    message=f"cue limit {max_cues} exceeded",
                    detail={"limit": max_cues},
                )
            )
            break
    return result


def _looks_like_index_line(line: str) -> bool:
    s = line.strip()
    return bool(s) and s.isdigit()


def _split_timing(line: str, fmt: str, lineno: int) -> tuple[Diagnostic | None, int, int]:
    if line.count("-->") != 1:
        return (
            Diagnostic(
                code="bad_timing_line",
                severity=Severity.ERROR,
                message=f"malformed timing line at line {lineno}: {line.strip()!r}",
                detail={"line": lineno, "raw": line.strip()[:80]},
            ),
            0,
            0,
        )
    left, right = line.split("-->")
    left_tok = left.strip()
    right_parts = right.split()
    right_tok = right_parts[0] if right_parts else ""
    if len(right_parts) > 1:
        code = "srt_positioning_not_supported" if fmt == FORMAT_SRT else "vtt_settings_not_supported"
        return (
            Diagnostic(
                code=code,
                severity=Severity.ERROR,
                message=f"timing line at line {lineno} carries positioning/cue settings, "
                f"which the subset does not allow: {line.strip()!r}",
                detail={"line": lineno, "raw": line.strip()[:120]},
            ),
            0,
            0,
        )
    try:
        start_ms = parse_ms(left_tok, fmt)
        end_ms = parse_ms(right_tok, fmt)
    except TimeParseError as exc:
        return (
            Diagnostic(
                code="invalid_timestamp",
                severity=Severity.ERROR,
                message=f"invalid timestamp at line {lineno}: {exc}",
                detail={"line": lineno, "raw": line.strip()[:120]},
            ),
            0,
            0,
        )
    return None, start_ms, end_ms
