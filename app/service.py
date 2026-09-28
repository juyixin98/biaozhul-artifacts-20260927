"""业务编排：解析媒体 -> 内核切段 -> 持久化 -> 区间输出与独立复核。

错误契约（app.errors 分类）:
    InputInvalidError       媒体/对齐/参数问题
    StateConflictError      作业已收尾仍写块等
    ResourceExhaustedError  超样本/请求体/作业数上限，或磁盘写入失败
    JobNotFoundError        未知 job
    ComputationFailedError  内核/重放不一致等内部失败（含锁一致性断言）
"""
from __future__ import annotations

import json
import threading
from typing import Optional

import numpy as np

from .config import Settings
from .errors import (ComputationFailedError, InputInvalidError,
                     ResourceExhaustedError, StateConflictError)
from .kernel import Interval, SegmentConfig, StreamingSegmenter
from .media import decode_pcm, parse_wav
from .reference import reference_segment, validate_intervals
from .schemas import SegmentConfigIn
from .store import STATUS_OPEN, JobStore
from .timing import ms_to_samples


def _loads(raw: str):
    return json.loads(raw)


def _interval_dict(iv: Interval, sample_rate: Optional[int] = None) -> dict:
    d = {"start": iv.start, "end": iv.end}
    if sample_rate:
        d["start_ms"] = iv.start * 1000.0 / sample_rate
        d["end_ms"] = iv.end * 1000.0 / sample_rate
    return d


def _chunk_cuts(n: int) -> list[list[tuple[int, int]]]:
    """若干语义不同的切块方案（整块/奇数/质数块宽），用于不变性重放。"""
    if n == 0:
        return [[(0, 0)]]
    out = [[(0, n)]]
    for width in (3, 7, 13, max(1, n // 3) + 1):
        cut = [(i, min(i + width, n)) for i in range(0, n, width)]
        if len(cut) > 1:
            out.append(cut)
    return out


class SegmentService:
    def __init__(self, store: JobStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self._segmenters: dict[str, StreamingSegmenter] = {}
        self._job_configs: dict[str, SegmentConfigIn] = {}
        self._seg_lock = threading.Lock()

    # ------------------------------------------------------------------ 配置

    def build_config(self, cfg_in: SegmentConfigIn,
                     sample_rate: int) -> SegmentConfig:
        enter, exit_ = cfg_in.effective_thresholds()
        return SegmentConfig(
            sample_rate=sample_rate,
            enter_threshold=float(enter),
            exit_threshold=float(exit_),
            min_speech=ms_to_samples(cfg_in.min_speech_ms, sample_rate),
            min_silence=ms_to_samples(cfg_in.min_silence_ms, sample_rate),
            pad_before=ms_to_samples(cfg_in.pad_before_ms, sample_rate),
            pad_after=ms_to_samples(cfg_in.pad_after_ms, sample_rate),
            merge_gap=ms_to_samples(cfg_in.merge_gap_ms, sample_rate),
            edge_keep=cfg_in.edge_keep)

    # ------------------------------------------------------------- 创建作业

    def create_job(self, cfg_in: SegmentConfigIn, media: dict,
                   run_id: str) -> str:
        container = media["container"]
        sr = media.get("sample_rate") if container == "pcm" else None
        ch = media.get("channels") if container == "pcm" else None
        job_id = self.store.create_job(
            cfg_in.model_dump(), media, sr, ch, self.settings.max_jobs)
        self._job_configs[job_id] = cfg_in
        self.store.add_event(job_id, run_id, "job_created",
                             {"container": container,
                              "sample_rate": sr, "channels": ch})
        return job_id

    # ------------------------------------------------------------- 写入数据

    def ingest_chunk(self, job_id: str, payload: bytes, run_id: str,
                     finalize: bool = False) -> dict:
        if len(payload) > self.settings.max_chunk_bytes:
            raise ResourceExhaustedError(
                f"chunk {len(payload)} bytes exceeds limit "
                f"{self.settings.max_chunk_bytes}",
                {"bytes": len(payload),
                 "max_chunk_bytes": self.settings.max_chunk_bytes})

        row = self.store.require_state(job_id, STATUS_OPEN)
        media = _loads(row["media_json"])
        if media["container"] == "wav":
            if finalize:
                raise StateConflictError(
                    "WAV jobs finalize automatically; do not pass finalize",
                    {"job_id": job_id})
            result = self._ingest_wav(job_id, payload, row, run_id)
        else:
            result = self._ingest_pcm(job_id, payload, row, media, run_id,
                                      finalize)
        return result

    def _ingest_pcm(self, job_id, payload, row, media, run_id,
                    finalize) -> dict:
        sf = media["sample_format"]
        sr, ch = media["sample_rate"], media["channels"]
        mono, frames = decode_pcm(payload, sf, ch)  # InputInvalidError
        self._guard_capacity(row, frames)
        cfg = self.build_config(self._cfg_in(row), sr)

        seg = self._get_or_restore_segmenter(row, cfg, media)
        before_locked = len(seg.locked_intervals)
        try:
            seg.push(mono)
        except ValueError as e:
            raise InputInvalidError(str(e)) from e
        except RuntimeError as e:
            raise ComputationFailedError(str(e)) from e

        self.store.add_chunk(job_id, payload, frames)
        self._flush_kernel_events(job_id, seg, run_id)
        self._sync_committed(job_id, seg)
        self.store.add_event(
            job_id, run_id, "chunk_accepted",
            {"frames": frames, "bytes": len(payload),
             "new_locked_intervals":
                 len(seg.locked_intervals) - before_locked})
        if finalize:
            return self.finalize(job_id, run_id)
        return self.describe(job_id)

    def _ingest_wav(self, job_id, payload, row, run_id) -> dict:
        if row["num_chunks"] > 0:
            raise StateConflictError(
                "WAV job accepts a single chunk containing the complete file",
                {"job_id": job_id, "num_chunks": row["num_chunks"]})
        mono, sr, ch, frames = parse_wav(payload)
        self._guard_capacity(row, frames)
        cfg_in = self._cfg_in(row)
        cfg = self.build_config(cfg_in, sr)
        self.store.add_chunk(job_id, payload, frames)

        events: list[dict] = []
        seg = StreamingSegmenter(cfg, on_event=events.append)
        try:
            seg.push(mono)
            intervals = seg.finish()
        except RuntimeError as e:
            self.store.mark_failed(
                job_id,
                {"code": "COMPUTATION_FAILED", "message": str(e)})
            raise ComputationFailedError(str(e)) from e
        for ev in events:
            self.store.add_event(job_id, run_id, "kernel", ev)
        out = [_interval_dict(iv) for iv in intervals]
        self.store.mark_finalized(job_id, out, frames, sr, ch)
        self.store.add_event(
            job_id, run_id, "wav_finalized",
            {"sample_rate": sr, "channels": ch, "frames": frames,
             "num_intervals": len(out)})
        self._drop_segmenter(job_id)
        return self.describe(job_id)

    # --------------------------------------------------------------- 收尾

    def finalize(self, job_id: str, run_id: str) -> dict:
        row = self.store.require_state(job_id, STATUS_OPEN)
        media = _loads(row["media_json"])
        if media["container"] == "wav":
            raise StateConflictError(
                "WAV jobs finalize automatically on their single chunk",
                {"job_id": job_id})
        sr = media["sample_rate"]
        cfg = self.build_config(self._cfg_in(row), sr)
        seg = self._get_or_restore_segmenter(row, cfg, media)
        try:
            intervals = seg.finish()
        except RuntimeError as e:
            self.store.mark_failed(
                job_id, {"code": "COMPUTATION_FAILED", "message": str(e)})
            raise ComputationFailedError(str(e)) from e
        out = [_interval_dict(iv) for iv in intervals]
        total = seg.total_samples
        self._flush_kernel_events(job_id, seg, run_id)
        self.store.mark_finalized(job_id, out, total, sr,
                                  media["channels"])
        self.store.add_event(
            job_id, run_id, "finalized",
            {"total_samples": total, "num_intervals": len(out)})
        self._drop_segmenter(job_id)
        return self.describe(job_id)

    # --------------------------------------------------------------- 查询

    def describe(self, job_id: str) -> dict:
        row = self.store.get_job(job_id)
        sr = row["sample_rate"]
        total = row["total_frames"]
        status = row["status"]
        if status == "finalized":
            intervals = [Interval(d["start"], d["end"])
                         for d in _loads(row["intervals_json"])]
            all_final = True
        else:
            seg = self._segmenters.get(job_id)
            if seg is not None:
                intervals = seg.locked_intervals
            else:
                # 进程重启后内存段不再存在；已锁定区间曾持久化在 committed_json
                intervals = [Interval(d["start"], d["end"])
                             for d in _loads(row["committed_json"])]
            all_final = False
        out = [_interval_dict(iv, sr) for iv in intervals]
        stats = None
        if status == "finalized":
            v = validate_intervals(intervals, total)
            stats = {"num_intervals": v["num_intervals"],
                     "kept_samples": v["kept_samples"],
                     "dropped_samples": v["dropped_samples"]}
        return {
            "job_id": job_id,
            "status": status,
            "container": _loads(row["media_json"])["container"],
            "sample_rate": sr,
            "channels": row["channels"],
            "total_samples": total,
            "duration_ms": (total * 1000.0 / sr) if sr else None,
            "intervals_committed": out,
            "all_intervals_final": all_final,
            "stats": stats,
            "error": _loads(row["error_json"]) if row["error_json"] else None,
        }

    def events(self, job_id: str, limit: int = 500) -> list[dict]:
        self.store.get_job(job_id)
        return self.store.list_events(job_id, limit)

    # --------------------------------------------------------------- 复核

    def verify(self, job_id: str, run_id: str) -> dict:
        """重放全部持久化块并做三类检查：

        1. 切块不变性：多种分块重跑内核，locked/final 区间两两相同；
        2. 完整性：有序、不重叠、不越界、样本统计守恒；
        3. 语义 oracle：独立参考实现（逐样本循环）逐区间相等
           （仅 finalized；open 作业尾部未决，不参与语义比较）。
        """
        row = self.store.get_job(job_id)
        media = _loads(row["media_json"])
        chunks = list(self.store.iter_chunks(job_id))
        checks: dict = {}
        mismatch: Optional[dict] = None
        finalized = row["status"] == "finalized"
        cfg_in = self._cfg_in(row)

        if media["container"] == "wav":
            if not chunks:
                raise ComputationFailedError("wav job has no chunk")
            mono, sr, ch, _frames = parse_wav(chunks[0][2])
            cfg = self.build_config(cfg_in, sr)
        else:
            sr = media["sample_rate"]
            ch = media["channels"]
            sf = media["sample_format"]
            cfg = self.build_config(cfg_in, sr)
            parts = []
            for _seq, _f, blob in chunks:
                m, _ = decode_pcm(blob, sf, ch)
                parts.append(m)
            mono = (np.concatenate(parts) if parts
                    else np.zeros(0, dtype=np.float64))

        # 1) 切块不变性
        cuts = _chunk_cuts(len(mono))
        replayed: list[list[Interval]] = []
        for cut in cuts:
            seg = StreamingSegmenter(cfg)
            for a, b in cut:
                seg.push(mono[a:b])
            replayed.append(seg.finish() if finalized
                            else seg.locked_intervals)
        cuts_consistent = all(r == replayed[0] for r in replayed[1:])
        checks["chunking_invariance"] = {
            "ok": cuts_consistent,
            "num_chunks_per_cut": [len(c) for c in cuts],
            "num_intervals_per_cut": [len(r) for r in replayed],
        }
        if not cuts_consistent:
            mismatch = {"type": "chunking",
                        "results": [[[iv.start, iv.end] for iv in r]
                                    for r in replayed]}

        # 2) 完整性
        target = replayed[0]
        integrity = validate_intervals(target, len(mono))
        checks["integrity"] = integrity
        checks["sample_conservation"] = {
            "ok": integrity["kept_samples"] + integrity["dropped_samples"]
            == len(mono)}
        if not integrity["ok"] and mismatch is None:
            mismatch = {"type": "integrity",
                        "violations": integrity["violations"]}

        # 3) 参考语义
        if finalized:
            ref = reference_segment(mono.tolist(), cfg, edge=True)
            stored = [Interval(d["start"], d["end"])
                      for d in _loads(row["intervals_json"])]
            sem_ok = ref == target == stored
            checks["reference_semantics"] = {
                "ok": sem_ok,
                "reference": [[iv.start, iv.end] for iv in ref],
                "kernel": [[iv.start, iv.end] for iv in target],
                "stored": [[iv.start, iv.end] for iv in stored],
            }
            if not sem_ok and mismatch is None:
                mismatch = {
                    "type": "reference_semantics",
                    "reference": [[iv.start, iv.end] for iv in ref],
                    "kernel": [[iv.start, iv.end] for iv in target],
                    "stored": [[iv.start, iv.end] for iv in stored]}

        ok = mismatch is None and all(c.get("ok", True)
                                      for c in checks.values())
        result = {"job_id": job_id, "ok": ok, "checks": checks,
                  "mismatch": mismatch}
        self.store.add_event(job_id, run_id, "verify",
                             {"ok": ok, "mismatch": mismatch})
        if not ok:
            raise ComputationFailedError(
                "verification found a mismatch", {"mismatch": mismatch})
        return result

    # ------------------------------------------------------------- 内部工具

    def _guard_capacity(self, row, frames: int) -> None:
        if row["total_frames"] + frames > \
                self.settings.max_samples_per_job:
            raise ResourceExhaustedError(
                "sample budget exceeded",
                {"total_frames": row["total_frames"],
                 "incoming": frames,
                 "max_samples_per_job":
                     self.settings.max_samples_per_job})

    def _cfg_in(self, row) -> SegmentConfigIn:
        """作业输入配置：优先内存，回退 SQLite（进程重启后）。"""
        jid = row["job_id"]
        cfg_in = self._job_configs.get(jid)
        if cfg_in is None:
            cfg_in = SegmentConfigIn(**_loads(row["config_json"]))
            self._job_configs[jid] = cfg_in
        return cfg_in

    def _get_or_restore_segmenter(self, row, cfg: SegmentConfig,
                                  media: dict) -> StreamingSegmenter:
        """取活跃段；进程重启后段不在内存时，重放已持久化的块恢复状态。"""
        job_id = row["job_id"]
        with self._seg_lock:
            seg = self._segmenters.get(job_id)
            if seg is not None:
                return seg
            seg = StreamingSegmenter(
                cfg, on_event=lambda ev: self._pending(job_id).append(ev))
            self._segmenters[job_id] = seg
        # 无锁重放历史块（作业级锁由调用路径保证不并发写同一 job）
        sf, sr, ch = (media["sample_format"], media["sample_rate"],
                      media["channels"])
        for _seq, _frames, blob in self.store.iter_chunks(job_id):
            mono, f = decode_pcm(blob, sf, ch)
            seg.push(mono)
        # 重放产生的事件已在历史中记录过，丢弃待写缓冲避免重复
        getattr(self, "_pending_events", {}).pop(job_id, None)
        return seg

    def _pending(self, job_id) -> list[dict]:
        if not hasattr(self, "_pending_events"):
            self._pending_events = {}
        return self._pending_events.setdefault(job_id, [])

    def _flush_kernel_events(self, job_id, seg, run_id) -> None:
        pending = getattr(self, "_pending_events", {}).pop(job_id, [])
        for ev in pending:
            self.store.add_event(job_id, run_id, "kernel", ev)

    def _sync_committed(self, job_id, seg) -> None:
        self.store.update_committed(
            job_id, [_interval_dict(iv) for iv in seg.locked_intervals])

    def _drop_segmenter(self, job_id) -> None:
        with self._seg_lock:
            self._segmenters.pop(job_id, None)
        getattr(self, "_pending_events", {}).pop(job_id, None)
