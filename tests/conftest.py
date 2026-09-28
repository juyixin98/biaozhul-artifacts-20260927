"""测试共享夹具。

包含两类与被测内核完全独立的东西：

1. :func:`oracle_segment_raw` —— 纯 Python、逐样本标量写的参考状态机，
   直接按需求文字实现，不 import 生产内核的任何函数。它是独立的“第二意见”，
   防止测试答案全部由被测核心自身生成。
2. 合成信号构造器 + 手工核验用常量（每个测试还会写明手工推导的区间）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.config import Settings
from app.logging_utils import RunLogger
from app.service import JobService
from app.store import JobStore


# ---------------------------------------------------------------------------
# 独立参考实现：逐样本标量状态机（仅用于测试，不依赖生产代码的内部机制）
# ---------------------------------------------------------------------------


def oracle_classify(x: float, enter: float, exit_: float) -> str:
    """三带分类，直接对应需求文字。"""
    a = abs(x)
    if a < enter:
        return "low"
    if a >= exit_:
        return "loud"
    return "mid"


def oracle_segment_raw(
    samples, *, enter: float, exit_: float, min_silence: int, min_activity: int
) -> list[tuple[int, int]]:
    """返回后处理前的原始有声区间（半开）。

    规则（与生产实现独立推导）：
      * silent 态：连续 loud 满 min_activity -> 退出，起点回溯到 loud 起点；
        low/mid 冲掉 loud 计数。
      * active 态：连续 low 满 min_silence -> 进入静音，区间在 low 起点闭合；
        mid/loud 冲掉 low 计数。
      * 结束仍 active 且曾确认活动 -> 尾部收到末尾。
    """
    state = "silent"
    seg_start: int | None = None
    confirmed = False
    low_run = 0
    low_start = -1
    loud_run = 0
    loud_start = -1
    out: list[tuple[int, int]] = []

    for i, x in enumerate(samples):
        band = oracle_classify(float(x), enter, exit_)
        if state == "active":
            if band == "low":
                if low_run == 0:
                    low_start = i
                low_run += 1
                if low_run >= min_silence:
                    if confirmed and seg_start is not None:
                        out.append((seg_start, low_start))
                    state = "silent"
                    seg_start = None
                    confirmed = False
                    low_run = 0
                    loud_run = 0
            else:  # mid 或 loud 都打断静音进入计数
                low_run = 0
        else:  # silent
            if band == "loud":
                if loud_run == 0:
                    loud_start = i
                loud_run += 1
                if loud_run >= min_activity:
                    state = "active"
                    seg_start = loud_start
                    confirmed = True
                    loud_run = 0
                    low_run = 0
            else:  # low 或 mid 都打断退出计数
                loud_run = 0

    if state == "active" and confirmed and seg_start is not None:
        out.append((seg_start, len(samples)))
    return out


def oracle_finalize(raw, total, pad, merge_gap):
    """参考后处理：加保留 -> 裁剪 -> 按间隔合并。"""
    padded = sorted((max(0, s - pad), min(total, e + pad)) for s, e in raw)
    merged: list[list[int]] = []
    for s, e in padded:
        if merged and s - merged[-1][1] <= merge_gap:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged if e > s]


# ---------------------------------------------------------------------------
# 合成信号
# ---------------------------------------------------------------------------


def signal_threshold_pulse() -> np.ndarray:
    """阈值附近脉冲场景（sr=1000）。

    布局：200 静音 + 5 MID(0.03) + 120 LOUD(0.10) + 5 MID + 300 LOW
    （够长 -> 确认进入静音）+ 10 LOUD 短噪声（< min_activity=100）+ 430 LOW。

    手工推导（min_silence=300, min_activity=100, pad=50）：
      raw 只有 [(205, 330)]；末尾短脉冲在 silent 态只累计 10 个 loud，
      随后被 low 冲掉，不产生第二区间、也不割裂静音。
      加保留并裁剪 => [(155, 380)]。
    """
    return np.array(
        [0.0] * 200 + [0.03] * 5 + [0.1] * 120 + [0.03] * 5
        + [0.0] * 300 + [0.1] * 10 + [0.0] * 430,
        dtype=np.float64,
    )


def signal_long_silence() -> np.ndarray:
    """跨块长静音场景（sr=1000）：100 LOUD + 600 LOW + 100 LOUD。

    手工推导：两段 raw=[(0,100),(700,800)]，间隔 600 样本 > 默认
    merge_gap(120) 与 pad(50*2=100 仍剩 500 间隔)，保留后
    [(0,150),(650,800)]。
    """
    return np.array([0.5] * 100 + [0.0] * 600 + [0.5] * 100, dtype=np.float64)


def signal_trailing_cases() -> dict[str, np.ndarray]:
    return {
        # 200 LOW + 100 LOUD：活动结束未遇静音 -> 末尾未完成段保留。
        "open_loud": np.array([0.0] * 200 + [0.5] * 100, dtype=np.float64),
        # 200 LOW + 100 LOUD + 100 LOW：尾部 LOW 不足 300，不闭合，仍保留。
        "unconfirmed_low": np.array(
            [0.0] * 200 + [0.5] * 100 + [0.0] * 100, dtype=np.float64
        ),
        # 200 LOW + 100 LOUD + 30 MID：迟滞带尾巴，仍属未完成段。
        "mid_tail": np.array(
            [0.0] * 200 + [0.5] * 100 + [0.03] * 30, dtype=np.float64
        ),
    }


# ---------------------------------------------------------------------------
# 服务 / 客户端夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=str(tmp_path / "data"),
        runs_log_path=str(tmp_path / "data" / "runs.jsonl"),
        max_samples_per_job=1_000_000,
        max_upload_bytes=4 * 1024 * 1024,
        stream_chunk_samples=4000,
    )


@pytest.fixture
def service(settings: Settings) -> JobService:
    store = JobStore(settings.data_dir)
    runs = RunLogger(settings.runs_log_path)
    return JobService(
        store,
        runs,
        max_samples_per_job=settings.max_samples_per_job,
        max_upload_bytes=settings.max_upload_bytes,
        stream_chunk_samples=settings.stream_chunk_samples,
        defaults={
            "min_silence_ms": settings.default_min_silence_ms,
            "min_activity_ms": settings.default_min_activity_ms,
            "pad_ms": settings.default_pad_ms,
            "merge_gap_ms": settings.default_merge_gap_ms,
            "enter_threshold": settings.default_enter_threshold,
            "exit_threshold": settings.default_exit_threshold,
        },
    )


@pytest.fixture
def client(service: JobService):
    from fastapi.testclient import TestClient

    from app.api import create_app

    return TestClient(create_app(service))


@pytest.fixture
def runs_logger(settings: Settings) -> RunLogger:
    return RunLogger(settings.runs_log_path)
