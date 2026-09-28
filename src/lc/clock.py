"""Clocks and the run recorder.

The kernel takes an explicit ``Clock`` so tests can exercise trust-period
boundaries deterministically. ``SystemClock`` is the wall-clock default;
``FixedClock``/``OffsetClock`` are test clocks.

``RunRecorder`` writes structured, replayable per-run logs: every verification
records its run id, the intermediate checks and the final decision reason.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...


class SystemClock:
    def now_ms(self) -> int:
        return int(time.time() * 1000)


class FixedClock:
    """Stands still until advanced. Used for boundary tests."""

    def __init__(self, now_ms: int):
        self._t = now_ms

    def now_ms(self) -> int:
        return self._t

    def set(self, now_ms: int) -> None:
        self._t = now_ms

    def advance_ms(self, delta_ms: int) -> None:
        self._t += delta_ms


class OffsetClock:
    """Wall clock shifted by a fixed offset (simulate a long-offline node)."""

    def __init__(self, offset_ms: int = 0):
        self.offset_ms = offset_ms

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.offset_ms


@dataclass
class RunRecorder:
    """Collects verification events for a run; optionally mirrors to a JSONL
    file so failures can be replayed from logs alone."""

    run_id: str = field(default_factory=lambda: f"run-{uuid.uuid4().hex[:12]}")
    log_dir: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.log_dir:
            os.makedirs(self.log_dir, exist_ok=True)
            self._path = os.path.join(self.log_dir, f"{self.run_id}.jsonl")
        else:
            self._path = None

    def record(self, event: str, **fields: Any) -> Dict[str, Any]:
        entry = {
            "run_id": self.run_id,
            "seq": len(self.events),
            "event": event,
            "time_ms": int(time.time() * 1000),
            **fields,
        }
        with self._lock:
            self.events.append(entry)
            if self._path:
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.events)

    @property
    def path(self) -> Optional[str]:
        return self._path
