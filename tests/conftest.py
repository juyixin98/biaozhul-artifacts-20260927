"""共享 pytest 夹具：读取由独立生成器产出的 JSON 真值。

注意：这些 JSON 由 tools/generate_fixtures.py 生成，该脚本不 import 任何
rsv.* 被测代码（只依赖 stdlib + cryptography），因此是外部参考答案。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FX = ROOT / "fixtures"


@pytest.fixture(scope="session")
def genesis() -> dict:
    return json.loads((FX / "genesis.json").read_text("utf-8"))


@pytest.fixture(scope="session")
def script_vectors() -> list[dict]:
    return json.loads((FX / "script_vectors.json").read_text("utf-8"))["vectors"]


@pytest.fixture(scope="session")
def keys_doc() -> dict:
    return json.loads((FX / "keys.json").read_text("utf-8"))


def load_bundle(name: str) -> dict:
    return json.loads((FX / "bundles" / name).read_text("utf-8"))
