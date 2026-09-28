"""Compatibility classification: incompatible inputs demand transcode."""
from app.core.compat import check_segments_compatibility
from app.errors import FailureCategory


def test_compatible_segments_have_no_problems(load):
    a = load("seg_ok_a.json")
    b = load("seg_ok_b.json")
    assert check_segments_compatibility([a, b]) == []


def test_timebase_mismatch_is_classified(load):
    a = load("seg_ok_a.json")
    tb = load("seg_tb_mismatch.json")
    problems = check_segments_compatibility([a, tb])
    cats = {(p.category, p.field) for p in problems}
    assert (FailureCategory.TIMEBASE_MISMATCH, "time_base") in cats
    values = next(p for p in problems
                  if p.category is FailureCategory.TIMEBASE_MISMATCH).values
    assert values == [[1, 90000], [1, 25]]


def test_codec_mismatch_is_classified(load):
    a = load("seg_ok_a.json")
    hc = load("seg_codec_mismatch.json")
    problems = check_segments_compatibility([a, hc])
    assert any(p.category is FailureCategory.CODEC_MISMATCH
               and p.field == "codec"
               and p.values == ["h264", "hevc"] for p in problems)


def test_every_incompatibility_demands_transcode(load):
    a = load("seg_ok_a.json")
    for name in ("seg_tb_mismatch.json", "seg_codec_mismatch.json"):
        problems = check_segments_compatibility([a, load(name)])
        assert problems, name
        assert all(p.to_dict()["requirement"] == "transcode"
                   for p in problems)


def test_single_segment_is_always_compatible(load):
    assert check_segments_compatibility([load("seg_open_gop.json")]) == []
