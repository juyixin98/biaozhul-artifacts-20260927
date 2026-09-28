"""版本存储边界与持久化测试。

* 裁剪后恰好在水位版本上提交可成功，水位-1 被拒绝；
* 快照前向重放得到正确历史文本；
* SQLite 落盘后关闭重开，文档/历史/水位/快照仍然存在。
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.errors import StaleBaseline
from app.models import Op
from app.service import OTService
from app.storage import Storage


def _add(svc, cid, seq, base, pos, ch, doc_len):
    return svc.submit("doc", cid, seq, base,
                      Op.insert_at(pos, ch, (cid, seq), doc_len))


def test_prune_horizon_boundary_submit(tmp_path):
    db = str(tmp_path / "b.db")
    svc = OTService(Storage(db), Settings(db_path=db))
    svc.create_document("doc", "")
    _add(svc, "c1", 1, 0, 0, "aa", 0)   # rev1 "aa"
    _add(svc, "c1", 2, 1, 2, "bb", 2)   # rev2 "aabb"
    _add(svc, "c1", 3, 2, 4, "cc", 4)   # rev3 "aabbcc"
    _add(svc, "c1", 4, 3, 6, "dd", 6)   # rev4 "aabbccdd"

    svc.prune("doc", 2)  # 水位=2，快照 "aabb"

    # 历史文本：水位本身可读（快照），水位之上可前向重放
    assert svc.text_at("doc", 2) == "aabb"
    assert svc.text_at("doc", 3) == "aabbcc"
    assert svc.text_at("doc", 4) == "aabbccdd"

    # 水位-1 彻底不可读
    with pytest.raises(StaleBaseline):
        svc.text_at("doc", 1)

    # 恰在水位 rev2 上提交：基线长度解析必须命中快照长度 4
    r = _add(svc, "c2", 1, 2, 0, "Z", 4)
    assert r.text == "Zaabb" + "ccdd"


def test_sqlite_persistence_across_reopen(tmp_path):
    db = str(tmp_path / "p.db")
    svc = OTService(Storage(db), Settings(db_path=db))
    svc.create_document("doc", "init")
    # 直接在 "init"(rev0) 上插入并裁剪
    svc.submit("doc", "c1", 1, 0,
               Op.insert_at(4, "!", ("c1", 1), 4))
    svc.prune("doc", 1)
    diag1 = svc.diagnostics()
    svc._db.close()

    # 重新打开：head、水位、快照文本、空操作历史都应保持
    svc2 = OTService(Storage(db), Settings(db_path=db))
    doc = svc2.get_document("doc")
    assert doc["text"] == "init!"
    assert doc["head_revision"] == 1
    assert doc["pruned_horizon"] == 1
    pulled = svc2.pull("doc", 1)
    assert pulled.ops == []  # rev1 已被裁剪进快照
    assert svc2.text_at("doc", 1) == "init!"
    # 旧基线依旧拒绝
    with pytest.raises(StaleBaseline):
        svc2.pull("doc", 0)
    # 重开后还能继续追加新版本
    r = svc2.submit("doc", "c1", 2, 1,
                    Op.insert_at(5, "?", ("c1", 2), 5))
    assert r.text == "init!?"
    svc2._db.close()
