"""Structured JSONL logging.

Every line carries a run_id (so logs can be correlated with a request or a
test run), component versions, progress step, and an explicit verdict. Unknown
states are never folded into "success": ``event`` distinguishes
``started`` / ``completed`` / ``rejected`` / ``failed``.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

from arrowzero.versions import runtime_versions

_LOCK = threading.Lock()


class RunLogger:
    def __init__(self, path: str | Path | None = None, *, echo: bool = True) -> None:
        self.path = Path(path) if path else None
        self.echo = echo
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        run_id: str,
        event: str,
        *,
        step: str,
        verdict: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "run_id": run_id,
            "event": event,
            "step": step,
            "verdict": verdict,
            "versions": runtime_versions(),
            "detail": detail or {},
        }
        line = json.dumps(record, sort_keys=True, default=str)
        with _LOCK:
            if self.path:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            if self.echo:
                sys.stderr.write(line + "\n")
                sys.stderr.flush()
