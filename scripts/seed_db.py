"""把合成夹具播种进 SQLite 版本存储。

用法::

    python -m scripts.seed_db                 # 默认数据文件，先 reset 再播种
    POSTING_DB_PATH=/tmp/x.sqlite python -m scripts.seed_db

产生的版本：
  v1 初始空版本（空全集）—— 由存储初始化
  v2 完整合成语料
  v3 在 v2 上删除文档（删除同步全集可见性）
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import get_settings  # noqa: E402
from app.storage.version_store import VersionStore  # noqa: E402

FIXTURE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "samples", "fixture.json")
)


def seed(db_path: str | None = None) -> int:
    settings = get_settings()
    db_path = db_path or settings.db_path
    with open(FIXTURE, encoding="utf-8") as fh:
        fixture = json.load(fh)

    store = VersionStore(db_path)
    store.reset()

    v2 = fixture["versions"][1]
    adds = [
        (doc_id, [term for term, docs in v2["terms"].items() if doc_id in docs])
        for doc_id in v2["universe"]
    ]
    vid2 = store.commit(
        parent_id=1, adds=adds, deletes=[], message="播种完整合成语料"
    )

    v3 = fixture["versions"][2]
    vid3 = store.commit(
        parent_id=vid2,
        adds=[],
        deletes=v3["deletes"],
        message="删除文档以验证全集可见性同步",
    )

    versions = store.list_versions()
    for info in versions:
        print(
            f"  v{info.version_id} parent={info.parent_id} "
            f"docs={info.visible_count}/{info.doc_count} {info.message}"
        )
    print(f"seed complete: db={db_path} v2={vid2} v3={vid3}")
    store.close()
    return vid3


if __name__ == "__main__":
    seed()
