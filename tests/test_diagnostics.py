from pathlib import Path

from app.kernel.diagnostics import diagnose
from app.parsing import parse_document

FIX = Path(__file__).resolve().parent.parent / "fixtures"


def _codes(diags):
    return [d.code for d in diags]


def test_chained_overlap_two_pairs(rlog):
    doc = parse_document((FIX / "chained_overlap.srt").read_text(encoding="utf-8"), "srt")
    diags = diagnose(doc.cues, min_duration_ms=1000)
    overlaps = [d for d in diags if d.code == "OVERLAP"]
    assert len(overlaps) == 2
    assert overlaps[0].cue_indices == [1, 2]
    assert overlaps[0].details["overlap_ms"] == 500
    assert overlaps[1].cue_indices == [2, 3]
    assert overlaps[1].details["overlap_ms"] == 500
    rlog("diagnostics_case", case="chained_overlap",
         codes=_codes(diags), expected="2 OVERLAP pairs [1,2] and [2,3]",
         verdict="matched")


def test_same_start_flagged():
    doc = parse_document((FIX / "same_start.vtt").read_text(encoding="utf-8"), "vtt")
    diags = diagnose(doc.cues, min_duration_ms=1000)
    overlaps = [d for d in diags if d.code == "OVERLAP"]
    assert len(overlaps) == 1
    assert overlaps[0].details["same_start"] is True
    # cue 2 starts 2000ms before cue 1's end (cue 1 runs [0,2000])
    assert overlaps[0].details["overlap_ms"] == 2000


def test_negative_and_zero_duration_separate():
    text = ("1\n00:00:02,000 --> 00:00:01,000\nneg\n\n"
            "2\n00:00:03,000 --> 00:00:03,000\nzero\n")
    doc = parse_document(text, "srt")
    diags = diagnose(doc.cues, min_duration_ms=1000)
    assert _codes(diags) == ["NEGATIVE_DURATION", "ZERO_DURATION"]
    assert diags[0].details["duration_ms"] == -1000


def test_too_short_is_warning():
    text = "1\n00:00:00,000 --> 00:00:00,400\nshort\n"
    doc = parse_document(text, "srt")
    diags = diagnose(doc.cues, min_duration_ms=1000)
    assert _codes(diags) == ["TOO_SHORT"]
    assert diags[0].severity == "warning"
    assert diags[0].details == {"duration_ms": 400, "required_ms": 1000}


def test_media_boundary_crossing():
    text = "1\n00:00:03,000 --> 00:00:05,000\nlate\n"
    doc = parse_document(text, "srt")
    diags = diagnose(doc.cues, min_duration_ms=1000, media_duration_ms=4000)
    assert _codes(diags) == ["MEDIA_BOUNDARY_EXCEEDED"]
    assert diags[0].details["media_duration_ms"] == 4000


def test_clean_file_has_no_diagnostics():
    doc = parse_document((FIX / "clean.vtt").read_text(encoding="utf-8"), "vtt")
    assert diagnose(doc.cues, min_duration_ms=1000) == []
