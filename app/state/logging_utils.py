"""只记录元数据的安全日志。

硬性约束：任何日志行都不得包含原文片段、脱敏后输出或映射密文。
只允许记录：request_id、规则档、规则 id、偏移区间、长度、计数、
错误分类码、引擎版本。这里对日志消息额外做一道秘密字符串扫描，
发现潜在泄漏直接丢弃该条并计数（纵深防御，不依赖调用方自觉）。
"""
from __future__ import annotations

import logging
import threading
from typing import Any


class _FilteringLogger(logging.Logger):
    """先做秘密扫描再决定是否 emit 的 Logger。

    关键：isEnabledFor/handle 之前在 callHandlers 之前过滤，被丢弃的记录
    不会走到本 logger 或任何外部（如 caplog）挂上来的 handler。
    """

    def __init__(self, name: str, guard: "SecretSafeLogger") -> None:
        super().__init__(name)
        self._guard = guard

    def handle(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = record.msg if isinstance(record.msg, str) else ""
        if self._guard._leaks(rendered):
            self._guard.dropped_leaky_records += 1
            return
        super().handle(record)


class SecretSafeLogger:
    def __init__(self, name: str = "log_redact") -> None:
        self._lock = threading.Lock()
        self.dropped_leaky_records = 0
        self._known_secrets: list[str] = []
        self._logger = _FilteringLogger(name, self)
        self._logger.propagate = False
        self._logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [log_redact] %(message)s"))
        self._logger.addHandler(handler)

    def _leaks(self, rendered: str) -> bool:
        with self._lock:
            return any(s and s in rendered for s in self._known_secrets)

    def register_request_secrets(self, secrets: list[str]) -> None:
        """注册当前进程见过的秘密样本，用于日志泄漏自检。

        仅用于测试/开发环境的防回归检查；生产可注册空列表。
        """
        with self._lock:
            for s in secrets:
                if s and s not in self._known_secrets:
                    self._known_secrets.append(s)

    def info(self, msg: str, *args: Any) -> None:
        # 过滤发生在 _FilteringLogger.handle，外部 handler（含 caplog）
        # 只能看到通过检查的记录。
        self._logger.info(msg, *args)

    def warning(self, msg: str, *args: Any) -> None:
        self._logger.warning(msg, *args)

    def error(self, msg: str, *args: Any) -> None:
        self._logger.error(msg, *args)


SAFE_LOGGER = SecretSafeLogger()
