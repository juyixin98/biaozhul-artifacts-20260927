"""示例：两个本地客户端在脚本化乱序网络下编辑同一文档，验证收敛。

运行：
    . .venv/bin/activate
    python examples/two_clients_demo.py

不依赖网络与真实账号，全部使用内存夹具。
"""

from __future__ import annotations

import sys
from itertools import permutations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from otbackend.clientsim import ScriptedNetwork, SimClient
from otbackend.repository import MemoryRepository
from otbackend.service import OTService

INITIAL = "abc"
EDITS = {
    "alice": [("ins", 0, "A")],
    "bob": [("ins", 1, "B")],
    "carol": [("del", 0, 2)],
}


def build_clients(net):
    clients = {}
    for cid in EDITS:
        c = SimClient(cid, net, "doc", INITIAL)
        for kind, p, x in EDITS[cid]:
            if kind == "ins":
                c.local_insert(p, x)
            else:
                c.local_delete(p, x)
        c.flush()
        clients[cid] = c
    return clients


def main() -> None:
    print(f"初始文档: {INITIAL!r}")
    print("三个客户端各自基于 r0 产生并发编辑，尝试全部 6 种接收顺序：\n")
    finals = set()
    for order in permutations(["alice", "bob", "carol"]):
        svc = OTService(MemoryRepository())
        svc.create_document("doc", INITIAL)
        net = ScriptedNetwork(svc)
        clients = build_clients(net)
        # order 用客户端名字映射到初始队列下标
        label_index = {f"{c}#1": i for i, c in enumerate(["alice", "bob", "carol"])}
        idx_order = [label_index[f"{c}#1"] for c in order]
        net.deliver_all(idx_order)
        for c in clients.values():
            c.poll()
        server = svc.get_document("doc").text
        views = {c: clients[c].text for c in clients}
        ok = len(set(views.values()) | {server}) == 1
        finals.add(server)
        print(f"  顺序 {order}: 服务端={server!r}  客户端一致={'是' if ok else '否'}")
    print(f"\n{6} 种顺序的最终文档集合: {finals}")
    assert len(finals) == 1, "未收敛！"
    print("结论：任意接收顺序都收敛到同一文档。")


if __name__ == "__main__":
    main()
