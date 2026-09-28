"""WebVTT restricted-subset parser.

Supported: WEBVTT header (+ metadata lines), NOTE comments (preserved),
cues with optional identifier and cue settings.
Rejected explicitly: STYLE / REGION blocks (outside the subset).
"""
from app.errors import SubtitleParseError
from app.parsing.model import Block, Cue, ParsedDocument
from app.parsing.timeparse import parse_timing_line


def parse_vtt(text: str) -> ParsedDocument:
    lines = text.split("\n")
    first = lines[0] if lines else ""
    if not (first == "WEBVTT" or first.startswith("WEBVTT ") or first.startswith("WEBVTT\t")):
        raise SubtitleParseError(
            "BAD_HEADER",
            "WebVTT content must start with a 'WEBVTT' header line",
            line_no=1,
            line_text=first,
        )
    blocks = [Block("header", lines=[first])]
    i = 1
    header_extra = []
    while i < len(lines) and lines[i].strip() != "":
        header_extra.append(lines[i])
        i += 1
    if header_extra:
        blocks.append(Block("header", lines=header_extra))

    cue_idx = 0
    while i < len(lines):
        if lines[i].strip() == "":
            i += 1
            continue
        start = i
        blines = []
        while i < len(lines) and lines[i].strip() != "":
            blines.append(lines[i])
            i += 1
        head = blines[0]
        if head == "NOTE" or head.startswith("NOTE ") or head.startswith("NOTE\t"):
            blocks.append(Block("note", lines=blines))
            continue
        if head.split()[0] in ("STYLE", "REGION"):
            raise SubtitleParseError(
                "UNSUPPORTED_BLOCK",
                f"{head.split()[0]} blocks are outside the supported WebVTT subset",
                line_no=start + 1,
                line_text=head,
            )
        if "-->" in head:
            ident, timing, timing_off, text_lines = "", head, 0, blines[1:]
        else:
            if len(blines) < 2 or "-->" not in blines[1]:
                raise SubtitleParseError(
                    "BAD_TIMING",
                    "cue timing line not found where expected",
                    line_no=start + 1,
                    line_text=head,
                )
            ident, timing, timing_off, text_lines = head, blines[1], 1, blines[2:]
        s, e, settings = parse_timing_line(timing, "vtt", line_no=start + 1 + timing_off)
        if not text_lines:
            raise SubtitleParseError(
                "EMPTY_CUE",
                "cue has no text lines",
                line_no=start + 1 + timing_off,
                line_text=timing,
            )
        cue_idx += 1
        blocks.append(Block("cue", cue=Cue(cue_idx, ident, s, e, list(text_lines), settings)))
    return ParsedDocument("vtt", blocks)
