"""测试共享夹具。

测试预言不使用被测核心：
- 扩展字素簇边界用第三方 regex 库的 \\X（独立 UAX#29 实现，与生产 grapheme 库不同源）；
- UTF-8 字节长度用手工编码规则独立计算；
- 所有参考文本均为显式字面量，期望值写死在断言里。
"""
from __future__ import annotations

import os
import sys

import pytest
import regex

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import create_app
from app.config import Settings
from app.service import TextIndexService
from app.storage import VersionStore


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=str(tmp_path / "test.db"),
        max_bytes=4096,
        max_codepoints=2000,
        max_clusters=500,
        max_documents=10,
        verify_incremental=True,
    )


@pytest.fixture
def store(settings):
    s = VersionStore(settings.db_path)
    yield s
    s.close()


@pytest.fixture
def service(store, settings):
    return TextIndexService(store, settings)


@pytest.fixture
def client(settings, tmp_path):
    """FastAPI TestClient，独立临时 DB。"""
    from fastapi.testclient import TestClient

    store = VersionStore(str(tmp_path / "http.db"))
    app = create_app(store)
    with TestClient(app) as c:
        c._store = store  # type: ignore[attr-defined]
        yield c
    store.close()


# ── 独立预言 ─────────────────────────────────────────────────────────────────
def oracle_clusters(text: str) -> list[str]:
    """regex \\X：不调用 grapheme 库，也不调用 build_index。"""
    return [m.group() for m in regex.finditer(r"\X", text)]


def oracle_cp_utf8_len(ch: str) -> int:
    cp = ord(ch)
    if cp <= 0x7F:
        return 1
    if cp <= 0x7FF:
        return 2
    if cp <= 0xFFFF:
        return 3
    return 4


def oracle_byte_length(text: str) -> int:
    return sum(oracle_cp_utf8_len(ch) for ch in text)


def oracle_cluster_starts(text: str) -> list[int]:
    """regex \\X 簇起点（码点偏移），末尾追加 len(text)。"""
    starts = [0]
    pos = 0
    for cl in oracle_clusters(text):
        pos += len(cl)
        starts.append(pos)
    return starts


def oracle_byte_starts(text: str) -> list[int]:
    starts = [0]
    for ch in text:
        starts.append(starts[-1] + oracle_cp_utf8_len(ch))
    return starts
