"""配置加载（tomllib 为标准库，无额外依赖）。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Config:
    root: Path
    sqlite_path: str
    resource_alphabet: list[str]
    principals: list[str]
    include_anonymous: bool
    max_space_size: int
    trusted_key_path: str
    demo_private_key_path: str
    api_host: str
    api_port: int
    witness_limit_per_bucket: int
    max_trace_steps: int

    def abs_path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p


def load_config(path: str | Path | None = None) -> Config:
    root = Path(__file__).resolve().parent.parent
    cfg_path = Path(path) if path else root / "config.toml"
    with open(cfg_path, "rb") as fh:
        data: dict[str, Any] = tomllib.load(fh)

    storage = data.get("storage", {})
    universe = data.get("universe", {})
    crypto = data.get("crypto", {})
    api = data.get("api", {})

    sqlite_path = os.environ.get(
        "DIFF_DB_PATH", storage.get("sqlite_path", "data/diff.db")
    )

    return Config(
        root=root,
        sqlite_path=sqlite_path,
        resource_alphabet=list(universe.get("resource_alphabet", ["0", "1", "/", "-"])),
        principals=list(universe.get("principals", [])),
        include_anonymous=bool(universe.get("include_anonymous", True)),
        max_space_size=int(universe.get("max_space_size", 500_000)),
        trusted_key_path=crypto.get(
            "trusted_key_path", "fixtures/keys/submitter_public.pem"
        ),
        demo_private_key_path=crypto.get(
            "demo_private_key_path", "fixtures/keys/submitter_private.pem"
        ),
        api_host=api.get("host", "127.0.0.1"),
        api_port=int(api.get("port", 8080)),
        witness_limit_per_bucket=int(api.get("witness_limit_per_bucket", 20)),
        max_trace_steps=int(api.get("max_trace_steps", 24)),
    )
