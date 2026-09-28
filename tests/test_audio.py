"""Audio priming and tail-padding behaviour with hand-computed values."""
from app.core import audio

VIDEO_END_48K = 16000  # 1/3 s of video expressed in audio ticks


def test_full_window_keeps_priming_frame(load):
    a = load("seg_ok_a.json").stream("audio")
    sel = audio.select_audio(a, 0, VIDEO_END_48K)
    assert sel.kept == tuple(range(17))
    assert sel.priming == frozenset({0})      # pts -1024
    assert sel.padding == ()                    # presented to 16384 >= 16000


def test_tail_padding_count_and_stamps(load):
    b = load("seg_ok_b.json").stream("audio")
    sel = audio.select_audio(b, 0, VIDEO_END_48K)
    # 16 real frames present only up to 15360: one silence frame is added
    assert sel.kept == tuple(range(16))
    assert len(sel.padding) == 1
    pad = sel.padding[0]
    assert pad.duration == 1024
    assert pad.dts == 16384   # last real dts 15360 + 1024
    assert pad.pts == 15360


def test_two_frame_encoder_delay(load):
    s = load("seg_audio_delay.json").stream("audio")
    assert s.encoder_delay == 2048
    sel = audio.select_audio(s, 0, VIDEO_END_48K)
    assert sel.kept == tuple(range(18))
    assert sel.priming == frozenset({0, 1})    # pts -2048, -1024
    assert sel.padding == ()


def test_trim_window_overlap_and_lead_in(load):
    s = load("seg_ok_a.json").stream("audio")
    sel = audio.select_audio(s, 6400, 12800)
    assert sel.kept == (7, 8, 9, 10, 11, 12, 13)
    assert sel.priming == frozenset({7})       # frame 6144..7168 straddles 6400
    assert sel.padding == ()


def test_padding_uses_ceil_for_partial_gap(load):
    s = load("seg_ok_b.json").stream("audio")
    # require two extra frames: 15360 -> 18048 gap 2688 = 3*1024-ish...
    sel = audio.select_audio(s, 0, 18048)
    # ceil((18048 - 15360) / 1024) = ceil(2.625) = 3
    assert [p.pts for p in sel.padding] == [15360, 16384, 17408]
    assert all(p.duration == 1024 for p in sel.padding)
