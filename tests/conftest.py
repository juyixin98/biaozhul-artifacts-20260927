import os
import sys
import tempfile

import pytest

# 允许从仓库根目录导入 app 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.costs import cost_profile_from_dict  # noqa: E402
from app.lexicon import connect, create_version  # noqa: E402
from app.service import create_app  # noqa: E402

UNIT_COSTS = {
    "insert": 1.0,
    "delete": 1.0,
    "substitute": 1.0,
    "transpose": 1.0,
    "substitute_table": {},
}


@pytest.fixture
def unit_profile():
    return cost_profile_from_dict(UNIT_COSTS)


@pytest.fixture
def weighted_profile():
    # 刻意非对称/非单位：交换便宜、插入比删除贵、替换有字符对优惠
    return cost_profile_from_dict({
        "insert": 1.3,
        "delete": 0.8,
        "substitute": 1.1,
        "transpose": 0.6,
        "substitute_table": {
            "0": {"o": 0.4},
            "o": {"0": 0.9},
        },
    })


@pytest.fixture
def tmp_db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def settings(tmp_db_path):
    return Settings(
        db_path=tmp_db_path,
        seed_path="data/seed_lexicon.jsonl",
        default_threshold=2.0,
        uncertainty_margin=0.5,
        max_query_length=32,
        max_candidates_evaluated=2000,
        max_results=25,
    )


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setenv("SPELLCHECK_COSTS", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "costs.json"))
    from fastapi.testclient import TestClient

    app = create_app(settings)
    conn = app.state.conn
    words = [
        "the", "quick", "brown", "fox", "dog", "hello", "world",
        "spelling", "tomorrow", "receive", "accommodate", "python",
        "distance", "algorithm", "abacus",
    ]
    create_version(conn, [(w, 100 - i) for i, w in enumerate(words)],
                   version_id="test-v1", source="test")
    with TestClient(app) as c:
        yield c
