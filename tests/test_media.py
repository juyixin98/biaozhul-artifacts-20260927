"""媒体解析测试：sidecar 夹具与 ffprobe 适配器产出同一模型。"""
from __future__ import annotations

import json

import pytest

from mediaconcat.config import Settings
from mediaconcat.media import ProbeError, parse_sidecar, resolve_clip
from mediaconcat.models import ClipProbe


def test_sidecar_round_trip_models(raw):
    doc = raw("open_gop.media.json")
    clip = parse_sidecar("fixtures/open_gop.media.json")
    assert isinstance(clip, ClipProbe)
    assert clip.video is not None and clip.video.codec == "h264"
    assert clip.video.time_base == (1, 25)
    assert clip.cut_in_sec == 0.24
    # 原始 JSON 与解析模型的包数一致（不经规划器）
    assert len(clip.video.frames) == len(doc["video"]["frames"]) == 10
    assert clip.video.frames[6].references == [5, 4]


def test_sidecar_rejects_bad_timebase(tmp_path):
    bad = tmp_path / "x.media.json"
    bad.write_text(json.dumps({
        "source": "x",
        "video": {"codec": "h264", "codec_type": "video",
                  "time_base": [0, 0], "frames": []},
    }), encoding="utf-8")
    with pytest.raises(Exception):
        parse_sidecar(bad)


def test_resolve_clip_from_fixtures_dir():
    s = Settings(fixtures_dir="fixtures")
    clip = resolve_clip("tb_mixed_a", s)
    assert clip.video.time_base == (1, 25)


def test_resolve_missing_raises_probe_error():
    s = Settings(fixtures_dir="fixtures", allow_ffprobe=False)
    with pytest.raises(ProbeError):
        resolve_clip("does_not_exist_xyz", s)
