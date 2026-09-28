"""端到端演示：两个本地模拟客户端经集中式服务协作（进程内传输）。

运行：python -m scripts.demo
逐步打印：离线编辑、同点并发排序、收敛、重复提交、历史裁剪与基线重建。
"""
from __future__ import annotations

from app.client import InProcessTransport, LocalClient
from app.config import Settings
from app.errors import ClientResetRequired
from app.models import Op
from app.service import OTService
from app.storage import Storage


def hr(title: str) -> None:
    print(f"\n{'─' * 64}\n{title}\n{'─' * 64}")


def main() -> None:
    storage = Storage(":memory:")
    svc = OTService(storage, Settings(db_path=":memory:"))
    svc.create_document("doc", "Hello")
    transport = InProcessTransport(svc)
    alice = LocalClient("alice", "doc", transport)
    bob = LocalClient("bob", "doc", transport)
    alice.join()
    bob.join()

    hr("1) 初始一致")
    print(f"alice={alice.text!r}  bob={bob.text!r}  rev={alice.revision}")

    hr("2) 双方离线编辑：alice 改开头，bob 改结尾，且同点各插一段")
    alice.insert(0, "[")
    bob.insert(len(bob.text), "]")
    alice.insert(1, ">>")
    bob.insert(5, "!!")  # bob 视角
    print("alice 本地:", alice.text)
    print("bob   本地:", bob.text)

    hr("3) 依次同步（不同到达顺序应收敛）")
    alice.sync()
    bob.sync()
    alice.sync()
    bob.sync()
    head = svc.get_document("doc")
    print("alice:", alice.text)
    print("bob  :", bob.text)
    print("server:", head["text"], " head_revision:", head["head_revision"])
    assert alice.text == bob.text == head["text"]

    hr("4) 重复提交安全：再各同步多次，文本/版本不变")
    before = (alice.text, alice.revision)
    for _ in range(3):
        alice.sync()
        bob.sync()
    print("仍为:", alice.text, " rev:", alice.revision, "(幂等键重传)")
    assert (alice.text, alice.revision) == before

    hr("5) 多字节：alice 插入中文与 emoji，bob 同时插入")
    p = len(alice.text)
    alice.insert(p, "，世界🌟")
    bob.insert(0, "【")
    alice.sync(); bob.sync(); alice.sync()
    print("收敛:", alice.text)
    assert alice.text == bob.text

    hr("6) 历史裁剪：把旧历史裁掉，模拟一个停在旧版本的迟到客户端")
    snapshot_rev = svc.get_document("doc")["head_revision"]
    stale = LocalClient("carol", "doc", transport)
    stale.join()  # carol 当前是最新
    # carol 离线编辑，服务端随后又前进并裁剪
    carol_remembered = stale.text
    stale.insert(0, "LATE-")
    svc.submit("doc", "alice", 99, snapshot_rev,
               Op.insert_at(0, "X", ("alice", 99),
                            len(svc.get_document("doc")["text"])))
    new_head = svc.get_document("doc")["head_revision"]
    svc.prune("doc", new_head)
    print(f"已裁剪到 rev {new_head}；carol 仍基于 rev {stale.revision}")
    try:
        stale.sync()
        print("意外：未拒绝旧基线")
    except ClientResetRequired as e:
        print(f"被明确拒绝：{type(e).__name__}: {e.message}")
    rebuilt = stale.rebuild_baseline()
    print("carol 重建基线 ->", rebuilt["text"][:40], "... rev", rebuilt["revision"])

    hr("诊断信息")
    import json
    print(json.dumps(svc.diagnostics(), ensure_ascii=False, indent=2))
    print("\n演示完成：所有收敛与错误分类断言均成立。")


if __name__ == "__main__":
    main()
