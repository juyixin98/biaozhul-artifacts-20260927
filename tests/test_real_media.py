"""可选集成测试：ffprobe 适配器解析真实 ffmpeg 生成媒体。

运行前提：
  bash scripts/make_real_fixtures.sh
默认跳过（CI/无 ffmpeg 环境），设 MEDIACONCAT_RUN_REAL=1 启用。
该路径只验证“真实输入 → 同一模型 → 规划器”，开放 GOP 引用关系仍以
合成夹具为权威（ffprobe 单包字段不暴露跨 GOP 引用）。
"""
from __future__ import annotations

import os
import pathlib
import shutil

import pytest

from mediaconcat.media import parse_real_media
from mediaconcat.planner import plan_concat

REAL = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "real"
pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and os.environ.get("MEDIACONCAT_RUN_REAL")
         and (REAL / "real_a_25fps.mp4").exists()),
    reason="需 ffmpeg + 先运行 scripts/make_real_fixtures.sh + MEDIACONCAT_RUN_REAL=1",
)


def test_probe_real_media_shares_model():
    a = parse_real_media(REAL / "real_a_25fps.mp4")
    assert a.video is not None and a.video.codec == "h264"
    assert len(a.video.frames) == 50  # 25fps × 2s
    assert all(f.duration > 0 for f in a.video.frames)


def test_real_concat_different_timebases_plans_or_reports_transcode():
    a = parse_real_media(REAL / "real_a_25fps.mp4")
    b = parse_real_media(REAL / "real_b_30fps.mp4")
    plan = plan_concat([a, b], "mp4", job_id="real-1")
    # ffprobe 不暴露逐帧参考列表 → 无法确认边界闭合 GOP，保守要求转码
    # （绝不因“看起来首帧是关键帧”而伪装直拼）。
    assert plan.feasible is False
    assert plan.mode == "transcode_required"
    codes = [f.code for f in plan.errors()]
    from mediaconcat.models import FailureCode
    assert FailureCode.GOP_STRUCTURE_UNKNOWN in codes
    # 单个真实片段（无拼接边界）仍可规划，时间戳无损
    solo = plan_concat([a], "mp4", job_id="real-solo")
    assert solo.feasible is True and solo.mode == "concat_copy"
    for seg in solo.segments:
        dts = [s.out_dts for s in seg.samples]
        assert dts[0] >= 0 and dts == sorted(dts)
