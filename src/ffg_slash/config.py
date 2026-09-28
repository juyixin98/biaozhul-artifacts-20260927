"""Configuration loading (JSON files with environment overrides).

Config carries the chain domain, DB location, log level and the initial
validator epochs. Validator entries may give a hex pubkey directly, or a
``seed``/``derive`` label from which the synthetic key is computed locally.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .crypto import derive_seed, keypair_from_seed
from .encoding import PUBKEY_SIZE


class ConfigError(ValueError):
    pass


@dataclass
class ValidatorSpec:
    pubkey: bytes
    weight: int
    seed: bytes | None = None  # retained for local demo signing


@dataclass
class AppConfig:
    chain_id: int
    genesis_root: bytes
    epochs: dict[int, dict[bytes, int]]
    validator_seeds: dict[bytes, bytes]
    database: Path
    log_dir: Path
    log_level: str = "INFO"

    def raw_epochs_json(self) -> list[dict]:
        return [
            {"epoch": e,
             "members": [{"pubkey": pk.hex(), "weight": w} for pk, w in sorted(m.items())]}
            for e, m in sorted(self.epochs.items())
        ]


def _as_32_hex(value: str, field_name: str) -> bytes:
    try:
        data = bytes.fromhex(value)
    except ValueError as exc:
        raise ConfigError(f"{field_name} must be hex") from exc
    if len(data) != PUBKEY_SIZE:
        raise ConfigError(f"{field_name} must decode to 32 bytes")
    return data


def _parse_validator(entry: dict) -> ValidatorSpec:
    if "weight" not in entry or not isinstance(entry["weight"], int) or entry["weight"] <= 0:
        raise ConfigError("validator weight must be a positive integer")
    seed: bytes | None = None
    if "pubkey" in entry:
        pubkey = _as_32_hex(entry["pubkey"], "pubkey")
        if "seed" in entry:
            seed = _as_32_hex(entry["seed"], "seed")
    elif "seed" in entry:
        seed = _as_32_hex(entry["seed"], "seed")
        _, pubkey = keypair_from_seed(seed)
    elif "derive" in entry:
        if not isinstance(entry["derive"], str):
            raise ConfigError("derive must be a string label")
        seed = derive_seed(entry["derive"])
        _, pubkey = keypair_from_seed(seed)
    else:
        raise ConfigError("validator entry needs pubkey, seed, or derive")
    return ValidatorSpec(pubkey=pubkey, weight=entry["weight"], seed=seed)


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ConfigError("config root must be an object")

    chain_id = data.get("chain_id", 1)
    if not isinstance(chain_id, int) or chain_id < 0:
        raise ConfigError("chain_id must be a non-negative integer")
    genesis = _as_32_hex(data.get("genesis_root", "00" * 32), "genesis_root")

    epochs_raw = data.get("epochs", [])
    if not isinstance(epochs_raw, list):
        raise ConfigError("epochs must be a list")
    epochs: dict[int, dict[bytes, int]] = {}
    seeds: dict[bytes, bytes] = {}
    for item in epochs_raw:
        if not isinstance(item, dict) or not isinstance(item.get("epoch"), int):
            raise ConfigError("each epoch needs an integer 'epoch'")
        epoch_no = item["epoch"]
        members: dict[bytes, int] = {}
        for entry in item.get("validators", []):
            vspec = _parse_validator(entry)
            if vspec.pubkey in members:
                raise ConfigError(f"duplicate validator in epoch {epoch_no}")
            members[vspec.pubkey] = vspec.weight
            if vspec.seed is not None:
                seeds[vspec.pubkey] = vspec.seed
        epochs[epoch_no] = members

    db_path = Path(os.environ.get("FFG_DB_PATH", data.get("database", "data/ffg.sqlite3")))
    log_dir = Path(os.environ.get("FFG_LOG_DIR", data.get("log_dir", "logs")))
    log_level = os.environ.get("FFG_LOG_LEVEL", data.get("log_level", "INFO"))

    return AppConfig(
        chain_id=chain_id,
        genesis_root=genesis,
        epochs=epochs,
        validator_seeds=seeds,
        database=db_path,
        log_dir=log_dir,
        log_level=log_level,
    )
