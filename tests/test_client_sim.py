"""两个（含三方）本地模拟客户端的端到端收敛测试。

这些用例通过 :class:`LocalClient` 的真实同步循环（inflight/buffer、
transform、幂等重传）驱动服务层，断言具体最终文本，覆盖：
  * 双方离线编辑不同区域后同步收敛；
  * 同点并发插入按客户端身份稳定排序；
  * 多字节（中文/emoji）文档上的协作；
  * 重复同步（幂等重传）不产生重复内容；
  * 一方多个排队编辑 + 另一方插入；
  * 历史裁剪后旧基线客户端被明确拒绝（ClientResetRequired），重建后继续。
"""
from __future__ import annotations

import pytest

from app.client import InProcessTransport, LocalClient
from app.config import Settings
from app.errors import ClientResetRequired
from app.service import OTService
from app.storage import Storage


@pytest.fixture
def world(tmp_path):
    storage = Storage(str(tmp_path / "c.db"))
    svc = OTService(storage, Settings(db_path=":memory:", max_doc_chars=100_000))
    svc.create_document("doc", "")
    t = InProcessTransport(svc)
    c1 = LocalClient(client_id="c1", doc_id="doc", transport=t)
    c2 = LocalClient(client_id="c2", doc_id="doc", transport=t)
    return {"svc": svc, "t": t, "c1": c1, "c2": c2}


def test_disjoint_offline_edits_converge(world):
    c1, c2 = world["c1"], world["c2"]
    # 共同基线 "hello world"
    c1.insert(0, "hello world")
    c1.sync()
    c2.sync()
    assert c1.text == c2.text == "hello world"

    # 双方离线：c1 在开头插入，c2 在末尾插入
    c1.insert(0, "[")
    c2.insert(len(c2.text), "]")
    # 不同步，各自再做一笔
    c1.insert(1, "(")
    c2.insert(len(c2.text) - 0 + 0, ")")  # 末尾

    c1.sync()
    c2.sync()
    c1.sync()  # 再追一轮保证双方完全静止
    c2.sync()
    assert c1.revision == c2.revision == world["svc"].get_document("doc")["head_revision"]
    assert c1.text == c2.text
    # 内容包含各自的全部插入且原文本保留
    for frag in ("[", "(", ")", "]", "hello world"):
        assert frag in c1.text
    assert c1.text == "[(hello world])"


def test_same_point_offline_inserts_client_order_stable(world):
    c1, c2, svc = world["c1"], world["c2"], world["svc"]
    c1.insert(0, "base")
    c1.sync()
    c2.sync()
    # 都在位置 2 离线插入
    c1.insert(2, "AA")
    c2.insert(2, "BB")
    # 两种"谁先同步"都应收敛到同一文本
    c1.sync()
    c2.sync()
    c1.sync()
    final_a = c1.text
    assert c2.text == final_a
    # "base" 位置2（"ba|se"）；origin ("c1",seq) < ("c2",seq)，AA 排在 BB 前
    assert final_a == "baAABBse"
    assert final_a.index("AA") < final_a.index("BB")

    # 反向到达顺序的另一文档：重置再来
    storage2 = Storage(":memory:")
    svc2 = OTService(storage2)
    svc2.create_document("d", "base")
    t2 = InProcessTransport(svc2)
    d1 = LocalClient("c1", "d", t2)
    d2 = LocalClient("c2", "d", t2)
    d1.join(); d2.join()  # 预置非空文档：先取全文快照
    assert d1.text == d2.text == "base"
    d1.insert(2, "AA")
    d2.insert(2, "BB")
    d2.sync()  # c2 先
    d1.sync()
    d2.sync()
    assert d1.text == d2.text == final_a  # 与到达顺序无关


def test_multibyte_collaboration(world):
    c1, c2 = world["c1"], world["c2"]
    c1.insert(0, "你好ab")  # 5 码点
    c1.sync()
    c2.sync()
    c1.insert(2, "🌟")     # 在 '好' 后（位置2）
    c2.insert(3, "世界")    # c2 视角位置3 是 'a' 前
    c1.sync()
    c2.sync()
    c1.sync()
    assert c1.text == c2.text
    assert len(c1.text) == 7  # 你 好 🌟 a 世 界 b
    # 位置2插🌟、位置3（'a'前）插世界 → 你好🌟a世界b
    assert c1.text == "你好🌟a世界b"


def test_repeated_sync_is_idempotent_no_duplicates(world):
    c1, c2 = world["c1"], world["c2"]
    c1.insert(0, "abc")
    c1.sync()
    # 多次空同步与重复同步不得产生重复字符
    for _ in range(5):
        c2.sync()
    assert c2.text == "abc"
    # c2 也编辑并多次同步
    c2.insert(3, "Z")
    for _ in range(5):
        c2.sync()
        c1.sync()
    assert c1.text == c2.text == "abcZ"
    assert world["svc"].get_document("doc")["head_revision"] == 2


def test_one_client_buffers_multiple_edits(world):
    c1, c2 = world["c1"], world["c2"]
    c1.insert(0, "abc")
    c1.sync()
    c2.sync()
    # c1 离线连续三笔（inflight + 2 buffer），期间 c2 插一笔
    c1.insert(3, "1")
    c1.insert(4, "2")
    c1.insert(5, "3")
    c2.insert(0, "X")
    c2.sync()        # c2 先落
    c1.sync()        # c1 一次同步排空三笔
    c2.sync()
    c1.sync()
    assert c1.text == c2.text
    assert c1.text == "Xabc123"


def test_prune_forces_old_client_to_rebuild(world, tmp_path):
    c1, c2, svc, t = world["c1"], world["c2"], world["svc"], world["t"]
    c1.insert(0, "line1\n")
    c1.sync(); c2.sync()
    # c2 离线产生一个未确认编辑，同时 c1 推进多个版本并裁剪
    c2.insert(0, "OFFLINE-")
    c1.insert(len(c1.text), "line2\n"); c1.sync()
    c1.insert(len(c1.text), "line3\n"); c1.sync()
    head = svc.get_document("doc")["head_revision"]
    svc.prune("doc", head)  # 裁到 head：c2 的 rev 1 基线失效

    # c2 再同步：发送旧基线操作 → 必须明确报需重建（而不是悄悄错乱）
    with pytest.raises(ClientResetRequired):
        c2.sync()

    # 重建后 c2 与服务端一致（离线编辑被丢弃——这是裁剪的既定代价）
    state = c2.rebuild_baseline()
    assert state["dropped_edits"] is True
    assert c2.text == svc.get_document("doc")["text"]
    assert c2.revision == svc.get_document("doc")["head_revision"]

    # 重建后可继续正常协作
    c2.insert(len(c2.text), "c2again")
    c2.sync(); c1.sync()
    assert c1.text == c2.text
    assert c1.text.endswith("c2again")
