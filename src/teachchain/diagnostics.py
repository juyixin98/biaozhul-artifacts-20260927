"""结构化诊断：带请求标识与关键状态的接受/拒绝日志。

设计要点
--------
* 每条日志带 ``request_id``（由调用方传入；API 层中间件生成）、级别、错误码、
  简短原因和关键字段（gas、nonce、高度等），说明“为什么接受/拒绝/无法判定”；
* **确定性执行内核不接触时间/随机源**：request_id 来自宿主层注入，若调用方
  不传则用 ``-``，不会在内核里偷偷取时间；
* 脱敏：``pub_b64``/``sig_b64``/``code_b64`` 等大字段只记录长度或前 8 字符；
  地址本就是假名标识，原样保留以便对账。
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

SENSITIVE_KEYS = {"pub_b64", "sig_b64", "code_b64", "private_pem", "private_key"}


def redact(key: str, value: Any) -> Any:
    if key in SENSITIVE_KEYS and isinstance(value, str):
        return f"<redacted:{len(value)} chars>"
    if isinstance(value, str) and key.endswith("_b64"):
        return f"<b64:{len(value)} chars>"
    return value


def sanitize(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: redact(k, v) for k, v in fields.items()}


class Diagnostics:
    """最小结构化诊断器：输出 JSON 行，可注入 request_id。"""

    def __init__(self, logger_name: str = "teachchain", stream=None) -> None:
        self._log = logging.getLogger(logger_name)
        if not self._log.handlers:
            handler = logging.StreamHandler(stream or sys.stderr)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._log.addHandler(handler)
            self._log.setLevel(logging.INFO)
            self._log.propagate = False

    def __call__(self, level: str, code: str, fields: dict[str, Any] | None = None,
                 *, request_id: str = "-") -> None:
        record = {
            "level": level,
            "code": code,
            "request_id": request_id,
            **sanitize(fields or {}),
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        getattr(self._log, level if level in ("info", "warning", "error") else "info")(line)
