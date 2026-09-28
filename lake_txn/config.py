"""配置加载与仓库路径约定。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    root: Path
    db_path: Path
    staging_dir: Path
    quarantine_dir: Path
    orphan_grace_seconds: int = 300
    redact_fields: frozenset[str] = field(default_factory=frozenset)
    allowed_inbound_dirs: tuple[Path, ...] = ()

    def table_dir(self, table: str) -> Path:
        return self.root / "tables" / table

    def data_dir(self, table: str) -> Path:
        return self.table_dir(table) / "data"

    def request_staging_dir(self, request_id: str) -> Path:
        return self.staging_dir / request_id

    def ensure_dirs(self) -> None:
        for p in (
            self.root,
            self.staging_dir,
            self.quarantine_dir,
            self.root / "tables",
        ):
            p.mkdir(parents=True, exist_ok=True)


def load_settings(config_path: str | os.PathLike[str] | None = None) -> Settings:
    """从 TOML 加载配置；未指定时查找 LAKE_TXN_CONFIG，再退回内置本地默认。"""
    path = Path(config_path or os.environ.get("LAKE_TXN_CONFIG", "config/service.toml"))
    if not path.exists():
        return local_default()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    base = path.resolve().parent

    wh = raw.get("warehouse", {})
    root = _resolve(base, wh.get("root", "./.local-data/warehouse"))
    sec = raw.get("security", {})
    inbound = tuple(
        _resolve(base, p) for p in sec.get("allowed_inbound_dirs", [])
    )
    return Settings(
        root=root,
        db_path=root / wh.get("db_name", "metadata.sqlite3"),
        staging_dir=root / wh.get("staging_dir", "staging"),
        quarantine_dir=root / wh.get("quarantine_dir", "quarantine"),
        orphan_grace_seconds=int(wh.get("orphan_grace_seconds", 300)),
        redact_fields=frozenset(s.lower() for s in sec.get("redact_fields", [])),
        allowed_inbound_dirs=inbound,
    )


def local_default(root: str | Path = "./.local-data/warehouse") -> Settings:
    root = Path(root).resolve()
    return Settings(
        root=root,
        db_path=root / "metadata.sqlite3",
        staging_dir=root / "staging",
        quarantine_dir=root / "quarantine",
        redact_fields=frozenset({"ssn", "email", "token", "secret", "password"}),
        allowed_inbound_dirs=(),
    )


def _resolve(base: Path, p: str) -> Path:
    path = Path(p)
    return path.resolve() if path.is_absolute() else (base / path).resolve()
