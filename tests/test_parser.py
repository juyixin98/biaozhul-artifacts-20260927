"""Tests for the media parsing layer: SRT/VTT subset grammar and preservation."""
from __future__ import annotations

from pathlib import Path


from app.media import parse_bytes, parse_text
from app.core.models import Severity

FIX = Path(__file__).parent / "fixtures"


def codes(result):
    return sorted(d.code for d in result.diagnostics)


def error_codes(result):
    return sorted(d.code for d in result.diagnostics
                  if d.severity == Severity.ERROR)


def test_chain_overlap_fixture_parses_five_cues():
    raw = (FIX / "chain_overlap.srt").read_bytes()
    r = parse_bytes(raw)
    assert r.fmt == "srt"
    assert r.ok
    assert len(r.cues) == 5
    assert r.cues[0].start_ms == 1_000 and r.cues[0].end_ms == 3_000
    assert r.cues[1].start_ms == 2_500
    # text lines preserved verbatim
    assert "First cue, starts the chain." in r.cues[0].raw_lines


def test_srt_index_line_is_optional_in_subset():
    doc = "00:00:01,000 --> 00:00:02,000\nno index line\n"
    r = parse_text(doc, "srt")
    assert r.ok and len(r.cues) == 1
    assert r.cues[0].raw_lines == ("no index line",)


def test_bad_timestamp_fixture_is_fatal_with_specific_codes():
    raw = (FIX / "bad_timestamp.srt").read_bytes()
    r = parse_bytes(raw)
    assert not r.ok
    # cue 1 has seconds=61; cue 2 uses VTT dot notation
    assert error_codes(r).count("invalid_timestamp") == 2
    detail = next(d for d in r.diagnostics if d.code == "invalid_timestamp")
    assert detail.detail["line"] in (2, 6)


def test_srt_positioning_metadata_rejected():
    raw = (FIX / "srt_position.srt").read_bytes()
    r = parse_bytes(raw)
    assert not r.ok
    assert error_codes(r) == ["srt_positioning_not_supported"]
    assert r.cues == []


def test_vtt_header_required():
    r = parse_text("00:00:01.000 --> 00:00:02.000\nmissing header\n", "vtt")
    assert not r.ok
    assert error_codes(r) == ["invalid_header"]


def test_vtt_note_blocks_ignored_settings_rejected():
    good = "WEBVTT\n\nNOTE a comment\n\n00:00:01.000 --> 00:00:02.000\nhi\n"
    r = parse_text(good, "vtt")
    assert r.ok and len(r.cues) == 1

    bad = "WEBVTT\n\nSTYLE\n::cue { color: red }\n"
    r2 = parse_text(bad, "vtt")
    assert error_codes(r2) == ["vtt_block_not_supported"]


def test_vtt_cue_settings_after_timing_rejected():
    doc = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000 align:start size:50%\nhi\n"
    r = parse_text(doc, "vtt")
    assert error_codes(r) == ["vtt_settings_not_supported"]


def test_vtt_cue_identifier_preserved():
    doc = (
        "WEBVTT\n\nintro-cue\n00:00:01.000 --> 00:00:02.000\nhello\n\n"
        "7\n00:00:03.000 --> 00:00:04.000\nnumeric id\n"
    )
    r = parse_text(doc, "vtt")
    assert r.ok
    assert [c.identifier for c in r.cues] == ["intro-cue", "7"]


def test_markup_allowed_and_preserved():
    doc = ("1\n00:00:01,000 --> 00:00:03,000\n"
           "<b>bold</b> <i>italic</i> <u>under</u> "
           "<font color=\"#fff\">x</font>\n")
    r = parse_text(doc, "srt")
    assert r.ok, [d.message for d in r.diagnostics]
    assert r.cues[0].text.startswith("<b>bold</b>")  # untouched


def test_bad_markup_fixture_specific_codes():
    raw = (FIX / "bad_markup.vtt").read_bytes()
    r = parse_bytes(raw)
    assert not r.ok
    errs = error_codes(r)
    assert errs.count("unsupported_markup") == 2  # <c> and <v>
    assert "unpaired_tag" in errs                 # open <b> never closed


def test_unknown_tag_and_mismatched_close():
    doc = "1\n00:00:01,000 --> 00:00:02,000\n<ruby>x</i>\n"
    r = parse_text(doc, "srt")
    errs = error_codes(r)
    assert "unsupported_markup" in errs
    assert "unpaired_tag" in errs


def test_inline_timestamp_tag_rejected_in_vtt():
    doc = "WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n<00:00:02.000>karaoke style\n"
    r = parse_text(doc, "vtt")
    assert "unsupported_markup" in error_codes(r)


def test_multilingual_text_roundtrips_unchanged():
    raw = (FIX / "multilingual.srt").read_bytes()
    r = parse_bytes(raw)
    assert r.ok, [d.message for d in r.diagnostics]
    payloads = [c.text for c in r.cues]
    assert any("中文" in p and "한국어" in p for p in payloads) is False
    assert any("中文" in p for p in payloads)
    assert any("العربية" in p for p in payloads)
    assert any("한국어" in p for p in payloads)
    assert any("&#128512;" in p for p in payloads)  # entity left literal
    # join payloads back and confirm byte-level preservation against the source
    src = raw.decode("utf-8")
    for c in r.cues:
        for line in c.raw_lines:
            assert line in src


def test_bom_recorded_and_crlf_normalized():
    doc = "﻿1\r\n00:00:01,000 --> 00:00:02,000\r\nhi\r\n\r\n"
    r = parse_text(doc.lstrip("﻿"), "srt", had_bom=True)
    assert r.had_bom is True
    assert r.cues[0].raw_lines == ("hi",)


def test_invalid_utf8_emits_encoding_error():
    raw = (FIX / "bad_encoding.srt").read_bytes()
    r = parse_bytes(raw)
    assert not r.ok
    errs = error_codes(r)
    assert errs == ["encoding_error"]
    diag = next(d for d in r.diagnostics if d.code == "encoding_error")
    assert isinstance(diag.detail["byte_offset"], int)


def test_missing_timing_line():
    r = parse_text("1\njust a number and text, no arrow\n", "srt")
    assert error_codes(r) == ["missing_timing"]


def test_auto_detection_picks_vtt():
    r = parse_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n")
    assert r.fmt == "vtt"
    r2 = parse_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    assert r2.fmt == "srt"
