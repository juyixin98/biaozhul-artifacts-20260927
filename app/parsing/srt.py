"""SRT parser: counter line, timing line, 1+ text lines, blank-line separated."""
from app.errors import SubtitleParseError
from app.parsing.model import Block, Cue, ParsedDocument
from app.parsing.timeparse import parse_timing_line


def parse_srt(text: str) -> ParsedDocument:
    lines = text.split("\n")
    blocks = []
    cur = []
    cur_start = 0
    cue_idx = 0

    def flush():
        nonlocal cur, cur_start, cue_idx
        if not cur:
            return
        blines, start = cur, cur_start
        cur = []
        counter = blines[0].strip()
        if not counter.isdigit():
            raise SubtitleParseError(
                "BAD_COUNTER",
                f"SRT cue counter must be a non-negative integer, got {blines[0]!r}",
                line_no=start,
                line_text=blines[0],
            )
        if len(blines) < 2:
            raise SubtitleParseError(
                "BAD_TIMING",
                "cue block is missing its timing line",
                line_no=start,
                line_text=blines[0],
            )
        s, e, settings = parse_timing_line(blines[1], "srt", line_no=start + 1)
        text_lines = blines[2:]
        if not text_lines:
            raise SubtitleParseError(
                "EMPTY_CUE",
                "cue has no text lines",
                line_no=start + 1,
                line_text=blines[1],
            )
        cue_idx += 1
        blocks.append(Block("cue", cue=Cue(cue_idx, counter, s, e, list(text_lines), settings)))

    for i, ln in enumerate(lines, start=1):
        if ln.strip() == "":
            flush()
        else:
            if not cur:
                cur_start = i
            cur.append(ln)
    flush()
    return ParsedDocument("srt", blocks)
