"""结构化日志层。

日志要求（对应验收）：
* 每条日志可关联输入或运行身份（``run_id`` / ``input_fingerprint``）；
* 显示服务版本、进度或计算步骤及判定依据（step 事件）；
* 成功/失败/未知状态严格区分，异常不被记为成功。

输出为单行 JSON，便于机器检索；默认写 stderr，可配置写文件。
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import __version__

_run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "run_id", default=None
)
_input_fp_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "input_fingerprint", default=None
)


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


def bind_run(run_id: str | None = None, input_fingerprint: str | None = None):
    """返回 context token 对，用于在请求作用域内绑定身份。"""
    t1 = _run_id_var.set(run_id or new_run_id())
    t2 = (
        _input_fp_var.set(input_fingerprint)
        if input_fingerprint is not None
        else _input_fp_var.set(_input_fp_var.get())
    )
    return t1, t2


def current_run_id() -> str | None:
    return _run_id_var.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "service": "anon-risk",
            "service_version": __version__,
            "message": record.getMessage(),
        }
        run_id = _run_id_var.get()
        if run_id:
            payload["run_id"] = run_id
        input_fp = _input_fp_var.get()
        if input_fp:
            payload["input_fingerprint"] = input_fp
        # 附加结构化字段
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(file_path: str | Path | None = None, level: str = "INFO") -> None:
    root = logging.getLogger("anon")
    for h in list(root.handlers):
        try:
            h.close()
        finally:
            root.removeHandler(h)
    root.setLevel(level.upper())
    root.propagate = False

    handler: logging.Handler
    if file_path:
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(file_path, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


def get_logger(name: str = "anon") -> logging.Logger:
    return logging.getLogger(name if name.startswith("anon") else f"anon.{name}")


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, event, extra={"extra_fields": {"event": event, **fields}})


class StepLogger:
    """记录分析进度与判定依据。

    每个 step 是一条可检索事件，含序号、阶段、关键中间量与可选的判定理由。
    同时把步骤收集到内存列表，作为运行结果 ``computation_trace`` 的一部分
    （审计与测试可据此核验"为什么这样判定"）。
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or get_logger("steps")
        self.steps: list[dict[str, Any]] = []

    def step(
        self,
        stage: str,
        detail: str = "",
        *,
        basis: str = "",
        level: int = logging.INFO,
        **fields: Any,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "seq": len(self.steps) + 1,
            "stage": stage,
            "detail": detail,
        }
        if basis:
            entry["basis"] = basis
        if fields:
            entry["data"] = _json_safe(fields)
        self.steps.append(entry)
        log_event(
            self._logger,
            f"step:{stage}",
            level,
            seq=entry["seq"],
            stage=stage,
            detail=detail,
            basis=basis or None,
            **_json_safe(fields),
        )
        return entry

    def verdict(self, status: str, reason: str, **fields: Any) -> None:
        log_event(
            self._logger,
            "verdict",
            logging.INFO if status == "succeeded" else logging.WARNING,
            status=status,
            reason=reason,
            **_json_safe(fields),
        )
        self.step("verdict", reason, basis=reason, status=status, **fields)


def _json_safe(obj: Any) -> Any:
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        if isinstance(obj, dict):
            return {str(k): _json_safe(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_json_safe(v) for v in obj]
        return str(obj)
