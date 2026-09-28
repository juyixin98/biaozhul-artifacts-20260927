"""State isolation — one fresh directory per run.

Layout (all under the configured local workspace root):

    <workspace_root>/
      <run_id>/
        input/<safe-upload-name>   # the bytes under inspection
        output/                    # created ONLY after pre-flight passes;
                                   # removed if streaming extraction fails
        run.log                    # per-run structured log

The run id embeds a UTC timestamp and 8 random bytes, so concurrent runs and
re-runs never share state. Nothing here writes outside the workspace root.
"""
from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_RUNS = "runs"


def _new_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{secrets.token_hex(4)}"


@dataclass
class RunWorkspace:
    run_id: str
    root: Path
    input_dir: Path
    output_dir: Path
    log_path: Path

    @property
    def exists(self) -> bool:
        return self.root.exists()

    def remove(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class WorkspaceManager:
    def __init__(self, base: str | Path):
        self.base = Path(base).resolve()
        self.runs_root = self.base / _RUNS

    def initialize(self) -> None:
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def create_run(self) -> RunWorkspace:
        self.initialize()
        # Retry on the astronomically unlikely id collision.
        for _ in range(5):
            run_id = _new_run_id()
            root = self.runs_root / run_id
            try:
                os.mkdir(root, mode=0o700)
                break
            except FileExistsError:
                continue
        else:  # pragma: no cover
            raise RuntimeError("could not allocate unique run directory")
        input_dir = root / "input"
        os.mkdir(input_dir, mode=0o700)
        output_dir = root / "output"  # created later by the extractor
        return RunWorkspace(
            run_id=run_id,
            root=root,
            input_dir=input_dir,
            output_dir=output_dir,
            log_path=root / "run.log",
        )

    def get_run(self, run_id: str) -> RunWorkspace | None:
        root = (self.runs_root / run_id).resolve()
        # Contain run_id lookups inside the runs root.
        if self.runs_root not in root.parents and root != self.runs_root:
            return None
        if not root.exists():
            return None
        return RunWorkspace(
            run_id=run_id,
            root=root,
            input_dir=root / "input",
            output_dir=root / "output",
            log_path=root / "run.log",
        )

    def list_runs(self, limit: int = 50) -> list[str]:
        if not self.runs_root.exists():
            return []
        ids = sorted(p.name for p in self.runs_root.iterdir() if p.is_dir())
        return ids[-limit:]
