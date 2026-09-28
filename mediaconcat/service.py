"""服务层：解析输入 → 规划 → 持久化，集中处理未知异常（不吞错）。"""
from __future__ import annotations

import traceback
import uuid
from typing import Optional

from .config import Settings
from .jobstore import JobStore
from .logging_setup import LogBinding
from .media import ProbeError, resolve_clip
from .models import ClipProbe, ConcatPlan, FailureCode
from .planner import plan_concat


class PlanningService:
    def __init__(self, store: JobStore, settings: Settings, log: LogBinding):
        self._store = store
        self._settings = settings
        self._log = log

    def submit(self, sources: list[str], container: str,
               cuts: Optional[list[dict]] = None,
               job_id: Optional[str] = None) -> str:
        job_id = job_id or f"job-{uuid.uuid4().hex[:12]}"
        self._store.create(job_id, sources, container)
        self._run(job_id, sources, container, cuts)
        return job_id

    def _run(self, job_id: str, sources: list[str], container: str,
             cuts: Optional[list[dict]]) -> None:
        log = self._log.bind(job_id=job_id)
        try:
            self._store.update_status(job_id, "planning")
            clips: list[ClipProbe] = []
            for i, source in enumerate(sources):
                clip = resolve_clip(source, self._settings, log)
                if cuts and i < len(cuts) and cuts[i]:
                    clip.cut_in_sec = cuts[i].get("cut_in_sec")
                    clip.cut_out_sec = cuts[i].get("cut_out_sec")
                clips.append(clip)
            plan = plan_concat(clips, output_container=container,
                               job_id=job_id, settings=self._settings, log=log)
            self._store.save_plan(job_id, plan.model_dump_json())
        except ProbeError as exc:
            log.error("probe_failed", error=str(exc))
            self._store.fail(job_id, FailureCode.INPUT_NOT_FOUND.value, str(exc))
        except Exception as exc:  # noqa: BLE001 — 顶层兜底：未知错误显式落盘
            log.exception("internal_error", error=exc.__class__.__name__)
            self._store.fail(
                job_id, FailureCode.INTERNAL_ERROR.value,
                f"{exc.__class__.__name__}: {exc}\n{traceback.format_exc()}",
            )

    def get_plan(self, job_id: str) -> Optional[ConcatPlan]:
        row = self._store.get(job_id)
        if not row or not row["plan_json"]:
            return None
        return ConcatPlan.model_validate_json(row["plan_json"])
