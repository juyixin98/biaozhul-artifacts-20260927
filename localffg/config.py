"""Configuration layer.

Values can be loaded from a YAML file and overridden by environment variables
(prefix ``LOCALFFG_``). The configuration is deliberately small and explicit;
nothing here requires production accounts or external services.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Domain separator every signature is bound to. A vote signed for a different
# application/domain MUST NOT validate here (domain separation).
DEFAULT_DOMAIN = b"localffg/validator-vote/v1"

# Magic prefix used for our canonical encoding ("length-prefixed", self
# describing) so messages produced by a different encoding do not parse.
ENCODING_MAGIC = b"LFV1"


@dataclass(frozen=True)
class AppConfig:
    chain_id: str = "local-chain-0"
    epoch_length: int = 10
    db_path: str = "data/localffg.db"
    domain: bytes = field(default=DEFAULT_DOMAIN)
    http_host: str = "127.0.0.1"
    http_port: int = 8000
    allow_bootstrap_api: bool = True
    log_level: str = "INFO"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["domain"] = self.domain.decode("ascii")
        return d


def _env(name: str) -> str | None:
    return os.environ.get(f"LOCALFFG_{name}")


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load configuration from an optional YAML file, then apply env overrides.

    Unknown file / missing keys simply fall back to defaults — config errors
    are surfaced explicitly (bad integer overrides raise) rather than silently
    swallowed.
    """
    raw: dict[str, Any] = {}
    if path:
        p = Path(path)
        if p.exists():
            loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
            if loaded is not None and not isinstance(loaded, dict):
                raise ValueError(f"config {path}: top level must be a mapping")
            raw = loaded or {}

    domain = raw.get("domain", DEFAULT_DOMAIN.decode("ascii"))
    cfg = AppConfig(
        chain_id=str(raw.get("chain_id", "local-chain-0")),
        epoch_length=int(raw.get("epoch_length", 10)),
        db_path=str(raw.get("db_path", "data/localffg.db")),
        domain=domain.encode("ascii"),
        http_host=str(raw.get("http_host", "127.0.0.1")),
        http_port=int(raw.get("http_port", 8000)),
        allow_bootstrap_api=bool(raw.get("allow_bootstrap_api", True)),
        log_level=str(raw.get("log_level", "INFO")),
    )

    overrides: dict[str, Any] = {}
    if _env("CHAIN_ID") is not None:
        overrides["chain_id"] = _env("CHAIN_ID")
    if _env("EPOCH_LENGTH") is not None:
        overrides["epoch_length"] = int(_env("EPOCH_LENGTH") or "")
    if _env("DB_PATH") is not None:
        overrides["db_path"] = _env("DB_PATH")
    if _env("DOMAIN") is not None:
        overrides["domain"] = (_env("DOMAIN") or "").encode("ascii")
    if _env("HTTP_HOST") is not None:
        overrides["http_host"] = _env("HTTP_HOST")
    if _env("HTTP_PORT") is not None:
        overrides["http_port"] = int(_env("HTTP_PORT") or "")
    if _env("ALLOW_BOOTSTRAP_API") is not None:
        overrides["allow_bootstrap_api"] = (_env("ALLOW_BOOTSTRAP_API") or "").lower() in {"1", "true", "yes"}
    if _env("LOG_LEVEL") is not None:
        overrides["log_level"] = _env("LOG_LEVEL")

    return AppConfig(**{**asdict(cfg), **overrides}) if overrides else cfg
