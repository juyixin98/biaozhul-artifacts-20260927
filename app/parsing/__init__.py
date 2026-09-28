from app.parsing.model import Block, Cue, ParsedDocument
from app.parsing.render import render_document
from app.parsing.srt import parse_srt
from app.parsing.vtt import parse_vtt


def parse_document(text: str, fmt: str) -> ParsedDocument:
    if fmt == "srt":
        return parse_srt(text)
    if fmt == "vtt":
        return parse_vtt(text)
    raise ValueError(f"unsupported format {fmt!r}")
