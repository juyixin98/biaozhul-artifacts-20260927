"""GOP selection and reference closure with hand-computed expected sets."""
import pytest

from app.core import gop
from app.errors import FailureCategory, PlannerError


def test_window_selection_uses_presentation_time(load):
    seg = load("seg_ok_a.json")
    v = seg.stream("video")
    # pts in [12000, 24000): displayed frames 4,5,6(I6),7
    assert gop.presentation_window_indices(v, 12000, 24000) == [4, 5, 6, 8]


def test_closure_pulls_preroll_for_midgop_trim(load):
    seg = load("seg_midgop.json")
    v = seg.stream("video")
    sel = gop.select_video(v, 12000, 24000)
    # presented frame B7 (idx 8) needs P9 (idx 7), B4/B5 need P3 (idx 1),
    # P3 needs I0 (idx 0): closure = {0,1,4,5,6,7,8}
    assert sel.decode_order == (0, 1, 4, 5, 6, 7, 8)
    assert sel.preroll == frozenset({0, 1, 7})
    assert sel.presented == (5, 6, 4, 8)  # presentation order by pts


def test_open_gop_keeps_pre_keyframe_references(load):
    seg = load("seg_open_gop.json")
    v = seg.stream("video")
    sel = gop.select_video(v, 18000, 30000)
    # B7/B8 (idx 8/9) reference P3 (idx 1) which is before the non-IDR
    # recovery point K6 (idx 4): it MUST survive in the plan as pre-roll.
    assert 1 in sel.decode_order
    assert 1 in sel.preroll
    assert sel.decode_order == (0, 1, 4, 7, 8, 9)
    assert sel.preroll == frozenset({0, 1})
    assert 2 not in sel.decode_order and 3 not in sel.decode_order
    assert 5 not in sel.decode_order and 6 not in sel.decode_order


def test_untrimmed_open_gop_keeps_everything(load):
    seg = load("seg_open_gop.json")
    v = seg.stream("video")
    sel = gop.select_video(v, 0, 30000)
    assert sel.decode_order == tuple(range(10))
    assert sel.preroll == frozenset()


def test_dangling_reference_is_hard_failure(load):
    seg = load("seg_dangling_ref.json")
    v = seg.stream("video")
    with pytest.raises(PlannerError) as exc:
        gop.select_video(v, 0, 30000)
    assert exc.value.category is FailureCategory.MISSING_REFERENCE
    assert exc.value.context["missing_index"] == 99


def test_empty_window_is_reported(load):
    seg = load("seg_ok_a.json")
    with pytest.raises(PlannerError) as exc:
        gop.select_video(seg.stream("video"), 900000, 1000000)
    assert exc.value.category is FailureCategory.EMPTY_WINDOW
