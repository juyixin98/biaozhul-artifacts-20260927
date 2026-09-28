from pathlib import Path

import pytest

from app.errors import SubtitleParseError
from app.parsing import parse_document, render_document
from app.parsing.encoding import decode_subtitle_bytes

FIX = Path(__file__).resolve().parent.parent / "fixtures"


def test_srt_multibyte_text_preserved(rlog):
    content = (FIX / "multibyte.srt").read_text(encoding="utf-8")
    doc = parse_document(content, "srt")
    assert len(doc.cues) == 2
    assert doc.cues[0].text_lines == ["多语言字符测试 — cafés, naïve, Ελληνικά"]
    assert doc.cues[1].text_lines == ["<i>强调</i> と emoji 🎬🔥 ونص عربي"]
    out = render_document(doc, {1: (0, 1500), 2: (1500, 3000)})
    assert "00:00:00,000 --> 00:00:01,500" in out
    assert "00:00:01,500 --> 00:00:03,000" in out
    # markup and multibyte text survive the round trip byte-for-byte
    assert "<i>强调</i> と emoji 🎬🔥 ونص عربي" in out
    assert "多语言字符测试 — cafés, naïve, Ελληνικά" in out
    rlog("parse_case", case="multibyte", input_sha256_len=len(content),
         cues=len(doc.cues), verdict="text preserved byte-for-byte")


def test_vtt_note_identifier_settings_preserved():
    text = ("WEBVTT\n\nNOTE a comment\n\ncue-1\n"
            "00:00.000 --> 00:01.000 line:75%\nhello\n")
    doc = parse_document(text, "vtt")
    assert [b.kind for b in doc.blocks] == ["header", "note", "cue"]
    cue = doc.cues[0]
    assert cue.identifier == "cue-1"
    assert cue.settings == "line:75%"
    out = render_document(doc, {1: (500, 1500)})
    assert "NOTE a comment" in out
    assert "cue-1" in out
    # rendered timestamps use the canonical HH:MM:SS.mmm form
    assert "00:00:00.500 --> 00:00:01.500 line:75%" in out


def test_vtt_missing_header():
    with pytest.raises(SubtitleParseError) as ei:
        parse_document("00:00.000 --> 00:01.000\nhi\n", "vtt")
    assert ei.value.code == "BAD_HEADER"
    assert ei.value.line_no == 1


def test_vtt_style_block_rejected():
    with pytest.raises(SubtitleParseError) as ei:
        parse_document("WEBVTT\n\nSTYLE\n::cue { color: red }\n", "vtt")
    assert ei.value.code == "UNSUPPORTED_BLOCK"


def test_srt_bad_counter():
    with pytest.raises(SubtitleParseError) as ei:
        parse_document("x\n00:00:01,000 --> 00:00:02,000\nhi\n", "srt")
    assert ei.value.code == "BAD_COUNTER"
    assert ei.value.line_no == 1


def test_srt_missing_timing_line():
    with pytest.raises(SubtitleParseError) as ei:
        parse_document("1\njust some text\n", "srt")
    assert ei.value.code == "BAD_TIMING"


def test_srt_empty_cue_rejected():
    with pytest.raises(SubtitleParseError) as ei:
        parse_document("1\n00:00:01,000 --> 00:00:02,000\n", "srt")
    assert ei.value.code == "EMPTY_CUE"


def test_decode_utf8_bom():
    text = decode_subtitle_bytes(
        b"\xef\xbb\xbf1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    doc = parse_document(text, "srt")
    assert doc.cues[0].start_ms == 1000


def test_decode_invalid_encoding():
    with pytest.raises(SubtitleParseError) as ei:
        decode_subtitle_bytes(b"\xff\xfe\x00bad")
    assert ei.value.code == "INVALID_ENCODING"


def test_decode_crlf_normalized():
    text = decode_subtitle_bytes(b"1\r\n00:00:01,000 --> 00:00:02,000\r\nhi\r\n")
    doc = parse_document(text, "srt")
    assert doc.cues[0].text_lines == ["hi"]
