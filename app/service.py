"""作业服务：编排“媒体解析 -> 参数换算 -> 流式判定 -> 落库/日志”。

错误契约：本层所有可预期失败抛 :class:`SegmentError`，类别可区分：
媒体/参数 -> input，超限 -> resource，状态不对 -> state，非有限样本 ->
computation。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import numpy as np

from .errors import SegmentError
from .logging_utils import RunLogger
from .media import parse_audio
from .segmentation import (
    Params,
    SilenceSegmenter,
    finalize_intervals,
    validate_intervals,
)
from .store import JobRecord, JobStore
from .timing import ms_to_samples, samples_to_seconds


class JobService:
    def __init__(
        self,
        store: JobStore,
        runs: RunLogger,
        *,
        max_samples_per_job: int,
        max_upload_bytes: int,
        stream_chunk_samples: int,
        defaults: dict[str, Any],
    ) -> None:
        self.store = store
        self.runs = runs
        self.max_samples_per_job = int(max_samples_per_job)
        self.max_upload_bytes = int(max_upload_bytes)
        self.stream_chunk_samples = int(stream_chunk_samples)
        self.defaults = dict(defaults)

    # ---- 参数 ----------------------------------------------------------------

    def build_params(self, cfg: dict[str, Any], sample_rate: int) -> Params:
        enter = float(cfg["enter_threshold"])
        exit_ = float(cfg["exit_threshold"])
        p = Params(
            sample_rate=int(sample_rate),
            enter_threshold=enter,
            exit_threshold=exit_,
            min_silence=ms_to_samples(
                cfg["min_silence_ms"], sample_rate, name="min_silence_ms"
            ),
            min_activity=ms_to_samples(
                cfg["min_activity_ms"], sample_rate, name="min_activity_ms"
            ),
            pad=ms_to_samples(cfg["pad_ms"], sample_rate, name="pad_ms"),
            merge_gap=ms_to_samples(
                cfg["merge_gap_ms"], sample_rate, name="merge_gap_ms"
            ),
        )
        p.validate()
        return p

    # ---- 提交 + 执行（同步作业） ----------------------------------------------

    def submit(
        self,
        data: bytes,
        cfg: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> JobRecord:
        if len(data) > self.max_upload_bytes:
            raise SegmentError(
                "RESOURCE_EXHAUSTED",
                "uploaded file exceeds byte budget",
                bytes=len(data),
                limit=self.max_upload_bytes,
            )
        if idempotency_key:
            existing = self.store.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                if existing.config != cfg:
                    raise SegmentError(
                        "STATE_CONFLICT",
                        "idempotency_key was already used with a different config",
                        idempotency_key=idempotency_key,
                        existing_job_id=existing.id,
                    )
                # 幂等命中：同键同载荷直接返回既有作业，不重复计算。
                return existing

        job_id = f"job-{uuid.uuid4().hex[:12]}"
        run_id = self.runs.new_run_id()
        now = time.time()
        rec = JobRecord(
            id=job_id,
            run_id=run_id,
            status="PENDING",
            created_at=now,
            updated_at=now,
            config=cfg,
            idempotency_key=idempotency_key,
        )
        self.store.create(rec)
        self.store.save_audio(job_id, data)
        self._execute(job_id, data, cfg, run_id)
        return self.store.get(job_id)

    def _execute(
        self, job_id: str, data: bytes, cfg: dict[str, Any], run_id: str
    ) -> None:
        if not self.store.cas_status(
            "PENDING", "RUNNING", job_id, updated_at=time.time()
        ):
            raise SegmentError(
                "STATE_CONFLICT",
                "job is not PENDING; execution may already have happened",
                job_id=job_id,
            )

        log_base = {
            "run_id": run_id,
            "job_id": job_id,
            "config": cfg,
            "chunk_size_samples": self.stream_chunk_samples,
        }
        try:
            audio = parse_audio(
                data,
                fmt=str(cfg.get("fmt", "auto")),
                sample_rate=cfg.get("sample_rate"),
            )
            if audio.total_samples > self.max_samples_per_job:
                raise SegmentError(
                    "RESOURCE_EXHAUSTED",
                    "decoded sample count exceeds job budget",
                    samples=audio.total_samples,
                    limit=self.max_samples_per_job,
                )

            params = self.build_params(cfg, audio.sample_rate)
            segmenter = SilenceSegmenter(params)
            n = self.stream_chunk_samples
            for start in range(0, audio.total_samples, n):
                segmenter.process(audio.samples[start : start + n])
            raw = segmenter.finish()
            intervals = finalize_intervals(
                raw, audio.total_samples, params.pad, params.merge_gap
            )
            validate_intervals(intervals, audio.total_samples)

            kept = sum(e - s for s, e in intervals)
            self.store.cas_status(
                "RUNNING",
                "SUCCEEDED",
                job_id,
                updated_at=time.time(),
                sample_rate=audio.sample_rate,
                total_samples=audio.total_samples,
                params={
                    "sample_rate": audio.sample_rate,
                    "enter_threshold": params.enter_threshold,
                    "exit_threshold": params.exit_threshold,
                    "min_silence": params.min_silence,
                    "min_activity": params.min_activity,
                    "pad": params.pad,
                    "merge_gap": params.merge_gap,
                },
                intervals=[list(iv) for iv in intervals],
                raw_ranges=[list(r) for r in raw],
            )
            self.runs.record(
                {
                    **log_base,
                    "status": "SUCCEEDED",
                    "failure_category": None,
                    "result": {
                        "sample_rate": audio.sample_rate,
                        "total_samples": audio.total_samples,
                        "intervals": intervals,
                        "raw_ranges": raw,
                        "kept_samples": kept,
                        "duration_seconds": samples_to_seconds(
                            audio.total_samples, audio.sample_rate
                        ),
                        "source_format": audio.source_format,
                    },
                    "final_state": segmenter.snapshot(),
                    "events_tail": segmenter.events[-12:],
                }
            )
        except SegmentError as exc:
            self._fail(job_id, exc, log_base)
            raise

    def _fail(self, job_id: str, exc: SegmentError, log_base: dict[str, Any]) -> None:
        self.store.cas_status(
            "RUNNING",
            "FAILED",
            job_id,
            updated_at=time.time(),
            error_code=exc.code,
            error_category=exc.category,
            error_message=exc.message,
            error_details=exc.details,
        )
        self.runs.record(
            {
                **log_base,
                "status": "FAILED",
                "failure_category": exc.category,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
            }
        )

    # ---- 查询 ----------------------------------------------------------------

    def get(self, job_id: str) -> JobRecord:
        return self.store.get(job_id)

    def trace(self, run_id: str) -> dict[str, Any]:
        """复现入口：按 run_id 取日志条目；不存在报 JOB_NOT_FOUND。"""
        entry = self.runs.find_run(run_id)
        if entry is None:
            raise SegmentError(
                "JOB_NOT_FOUND", "no recorded run with that run_id", run_id=run_id
            )
        return entry

    # ---- 验证接口 --------------------------------------------------------------

    def verify(self, payload: dict[str, Any]) -> dict[str, Any]:
        """对合成信号独立核验：守恒、切块不变性、边界合法、原始区间重算一致。

        这里用“重算两遍”的方式验证切块不变性（多个互切块大小），并用一个
        与主状态机独立的简单区间检查器验证不重叠/不越界与样本守恒；该检查器
        不实现切段逻辑，只检查数学性质。
        """
        arr = np.asarray(payload["samples"], dtype=np.float64)
        rate = int(payload["sample_rate"])
        cfg = {
            "enter_threshold": float(payload["enter_threshold"]),
            "exit_threshold": float(payload["exit_threshold"]),
            "min_silence_ms": int(payload["min_silence_ms"]),
            "min_activity_ms": int(payload["min_activity_ms"]),
            "pad_ms": int(payload["pad_ms"]),
            "merge_gap_ms": int(payload["merge_gap_ms"]),
        }
        params = self.build_params(cfg, rate)
        base = SilenceSegmenter(params)
        base.process(arr)
        raw_base = base.finish()
        intervals = finalize_intervals(
            raw_base, arr.size, params.pad, params.merge_gap
        )

        chunk_sizes = [int(c) for c in payload.get("chunk_sizes", []) if int(c) > 0]
        variants: dict[str, list[list[int]]] = {}
        for c in chunk_sizes:
            seg = SilenceSegmenter(params)
            for start in range(0, arr.size, c):
                seg.process(arr[start : start + c])
            raw_c = seg.finish()
            iv_c = finalize_intervals(raw_c, arr.size, params.pad, params.merge_gap)
            variants[str(c)] = [list(x) for x in iv_c]

        check = check_conservation(intervals, arr.size)
        chunk_invariant = all(v == [list(x) for x in intervals] for v in variants.values())

        ok = bool(check["valid"]) and (
            all(v == [list(x) for x in intervals] for v in variants.values())
            if variants
            else True
        )
        return {
            "ok": ok,
            "total_samples": int(arr.size),
            "params": {
                "enter_threshold": params.enter_threshold,
                "exit_threshold": params.exit_threshold,
                "min_silence": params.min_silence,
                "min_activity": params.min_activity,
                "pad": params.pad,
                "merge_gap": params.merge_gap,
            },
            "raw_ranges": [list(r) for r in raw_base],
            "intervals": [list(iv) for iv in intervals],
            "conservation": check,
            "chunk_invariance": {
                "checked": bool(variants),
                "equal": chunk_invariant if variants else None,
                "variants": variants,
            },
        }


def check_conservation(
    intervals: list[tuple[int, int]], total_samples: int
) -> dict[str, Any]:
    """独立数学检查器（不实现切段）：非空/有序/不重叠/不越界 + 样本计数守恒。

    守恒口径：所有区间长度之和 ``kept`` 满足
    ``0 <= kept <= total``，且逐段计数 == 总和（区间不重叠时二者恒等，
    一旦重叠或越界即不等/越界）。
    """
    problems: list[str] = []
    kept_sum = 0
    prev_end = 0
    for i, (s, e) in enumerate(intervals):
        if not isinstance(s, (int, np.integer)) or not isinstance(e, (int, np.integer)):
            problems.append(f"interval[{i}] endpoints are not integer samples")
            continue
        s, e = int(s), int(e)
        if e <= s:
            problems.append(f"interval[{i}] empty or reversed: [{s},{e})")
        if s < 0 or e > total_samples:
            problems.append(f"interval[{i}] out of bounds: [{s},{e}) / {total_samples}")
        if s < prev_end:
            problems.append(f"interval[{i}] overlaps previous: [{s},{e})")
        kept_sum += max(0, e - s)
        prev_end = max(prev_end, e)
    if kept_sum > total_samples:
        problems.append("kept sample count exceeds total (conservation violated)")
    return {
        "valid": not problems,
        "problems": problems,
        "kept_samples": int(kept_sum),
        "total_samples": int(total_samples),
    }
