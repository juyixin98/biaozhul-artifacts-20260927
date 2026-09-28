"""共享夹具：直接加载 sidecar（独立于规划器的加载路径也做一次原始 JSON 校验）。"""
from __future__ import annotations

import json
import pathlib

import pytest

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def fixpath():
    def _p(name: str) -> pathlib.Path:
        p = FIX / name
        assert p.exists(), f"缺少夹具 {p}"
        return p
    return _p


@pytest.fixture
def raw(fixpath):
    def _raw(name: str) -> dict:
        return json.loads(fixpath(name).read_text(encoding="utf-8"))
    return _raw
