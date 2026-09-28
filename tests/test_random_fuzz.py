"""随机差分模糊测试。

两层：

1. :func:`test_realistic_two_client_histories_converge` 模拟真实同步纪律
   （每个客户端最多一个在途操作；服务端逐对 transform 落库），随机交错
   "编辑 / 同步"，断言两（三）个客户端最终与服务端一致。这是对生产服务
   重放路径的端到端差分。
2. :func:`test_global_nway_merge_is_order_independent` 针对 ≥3 个都基于
   同一版本的并发操作，验证 :func:`global_merge`（顺序无关的权威 N 路
   合并，含删除塌缩锚点情形）对全部到达排列给出同一文本。
"""
from __future__ import annotations

import itertools
import random

import pytest

from app.client import InProcessTransport, LocalClient
from app.config import Settings
from app.models import Op
from app.ot import apply, global_merge
from app.service import OTService
from app.storage import Storage


def _random_edit(rng: random.Random, text: str, client: str, seq: int):
    L = len(text)
    if rng.random() < 0.55 or L == 0:
        pos = rng.randrange(0, L + 1)
        return Op.insert_at(pos, rng.choice(["a", "b", "你", "🌟", "xy"]),
                            (client, seq), L)
    pos = rng.randrange(0, L)
    return Op.delete_range(pos, rng.randrange(1, L - pos + 1), L)


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 42, 100, 2026])
def test_realistic_two_client_histories_converge(seed, otlog):
    """随机交错编辑/同步：两个客户端 + 服务端，最终三方一致。"""
    rng = random.Random(seed)
    svc = OTService(Storage(":memory:"),
                    Settings(db_path=":memory:", max_doc_chars=10 ** 9))
    svc.create_document("doc", rng.choice(["", "abc", "你好🌟", "qwerty"]))
    t = InProcessTransport(svc)
    clients = [LocalClient("c1", "doc", t), LocalClient("c2", "doc", t)]
    for c in clients:
        c.join()

    actions = 0
    for _ in range(120):
        c = rng.choice(clients)
        can_edit = c.inflight is None
        if can_edit and rng.random() < 0.6:
            c._bump_seq()
            seq = c._seq
            op = _random_edit(rng, c.text, c.client_id, seq)
            c._enqueue_and_apply(seq, op)
        else:
            c.sync()  # 有在途操作时只能同步（遵守单 inflight 纪律）
        actions += 1
    # 排空：反复让所有客户端同步，直到没有人有待发编辑且都到达同一 head
    for _ in range(200):
        for c in clients:
            c.sync()
        head = svc.get_document("doc")["head_revision"]
        if all(not c.inflight and not c.buffer and c.revision == head
               for c in clients):
            break

    server = svc.get_document("doc")
    assert clients[0].text == clients[1].text == server["text"]
    assert clients[0].revision == clients[1].revision == server["head_revision"]
    otlog(
        "random-realistic",
        verdict="pass",
        reason=f"seed={seed}：{actions} 次随机编辑/同步后两客户端与服务端一致",
        inputs={"seed": seed, "actions": actions,
                "initial_doc": server["length"]},
        states={"final_text": server["text"], "head": server["head_revision"]},
    )


def _nway_ops(rng: random.Random, base: str, nclients: int):
    ops = []
    for k in range(nclients):
        L = len(base)
        cid = f"c{k}"
        if rng.random() < 0.6 or L == 0:
            ops.append(Op.insert_at(
                rng.randrange(0, L + 1), rng.choice(["a", "你", "🌟"]),
                (cid, 1), L))
        else:
            pos = rng.randrange(0, L)
            ops.append(Op.delete_range(pos, rng.randrange(1, L - pos + 1), L))
    return ops


@pytest.mark.parametrize("seed", [1, 16, 17, 42, 500, 999])
def test_global_nway_merge_is_order_independent(seed, otlog):
    """≥3 客户端各基于同一版本的并发操作：全局合并对所有到达排列同结果。"""
    rng = random.Random(seed)
    base = rng.choice(["abc", "你x", "qwerty", "ab"])
    ops = _nway_ops(rng, base, rng.choice([3, 4, 5]))
    results = {
        order: global_merge(base, [ops[i] for i in order])
        for order in itertools.permutations(range(len(ops)))
    }
    assert len(set(results.values())) == 1, (seed, results)
    otlog(
        "global-nway",
        verdict="pass",
        reason=f"seed={seed}：{len(ops)} 个并发操作的全部 {len(results)} 种"
               "到达顺序经全局合并收敛（含删除塌缩锚点）",
        inputs={"seed": seed, "base": base,
                "ops": [o.to_dict() for o in ops]},
        states={"converged_text": next(iter(results.values()))},
    )
