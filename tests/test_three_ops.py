"""部分三操作序列：全部 6 种接收顺序都必须收敛到独立 oracle。

操作不再逐字符枚举（组合爆炸），而是用一组精心选择的“有意思”
单组件操作：同点插入、重叠删除、插入落在删除区间内、多字节等。
从三方各取一个操作，对 3!=6 种接收顺序做全排列。

判定：6 种顺序的被测服务结果字符串两两相等，且等于独立
:class:`OracleServer` 在相同顺序下的 token 身份渲染。
"""

from __future__ import annotations

import itertools

import pytest

from otbackend.repository import MemoryRepository
from otbackend.service import OTService

from .oracle import OracleServer, marked_from_components
from .runlog import RunLogger

BASE = "abc"
N = len(BASE)


def _svc():
    s = OTService(MemoryRepository())
    s.create_document("d", BASE)
    return s


# 每个客户端的候选消息（单组件）。覆盖关键几何关系。
A_OPS = [
    [{"type": "ins", "pos": 0, "text": "A"}],
    [{"type": "ins", "pos": 1, "text": "a"}],
    [{"type": "ins", "pos": 3, "text": "尾"}],       # 多字节、文末
    [{"type": "del", "pos": 0, "length": 1}],
    [{"type": "del", "pos": 1, "length": 2}],       # 删 bc
]
B_OPS = [
    [{"type": "ins", "pos": 0, "text": "B"}],
    [{"type": "ins", "pos": 1, "text": "b"}],
    [{"type": "ins", "pos": 1, "text": "😀"}],
    [{"type": "del", "pos": 0, "length": 2}],       # 删 ab，与 A 重叠
    [{"type": "del", "pos": 2, "length": 1}],
]
C_OPS = [
    [{"type": "ins", "pos": 0, "text": "C"}],
    [{"type": "ins", "pos": 2, "text": "c"}],
    [{"type": "del", "pos": 1, "length": 1}],       # 删中间，与各方重叠
    [{"type": "del", "pos": 0, "length": 3}],       # 删全文
]

CLIENTS = [("alice", A_OPS), ("bob", B_OPS), ("carol", C_OPS)]


@pytest.fixture(scope="module")
def logger():
    log = RunLogger()
    yield log
    log.flush()


def test_three_op_all_permutations(logger):
    triples = list(itertools.product(A_OPS, B_OPS, C_OPS))
    checked = 0
    failures = []

    for ia, ib, ic in triples:
        raw = {"alice": ia, "bob": ib, "carol": ic}
        results = {}
        oracle_results = {}
        for order in itertools.permutations(["alice", "bob", "carol"]):
            svc = _svc()
            osrv = OracleServer(BASE)
            texts = []
            for cid in order:
                ack = svc.submit("d", 0, cid, 1, raw[cid])
                texts.append(ack.text)
                mo = marked_from_components(
                    _stamped_components(raw[cid], cid), cid, 1)
                osrv.submit(cid, 1, mo, N)
            from .oracle import render
            results[order] = (texts[-1], render(osrv.tokens))
            oracle_results[order] = render(osrv.tokens)

        uniq = {v[0] for v in results.values()}
        oracle_uniq = {v[1] for v in results.values()}
        if len(uniq) != 1:
            failures.append(("CONVERGE", raw, {k: v[0] for k, v in results.items()}))
            continue
        if len(oracle_uniq) != 1:
            failures.append(("ORACLE-CONVERGE", raw, oracle_results))
            continue
        if next(iter(uniq)) != next(iter(oracle_uniq)):
            failures.append(("MATCH", raw, uniq, oracle_uniq))
            continue
        checked += 1

    logger.record(
        suite="three-op-permutations",
        inputs={"triples": len(triples), "orders_each": 6},
        steps=[{"checked": checked, "failures": len(failures)}],
        verdict="PASS" if not failures else "FAIL",
        reason=(f"{checked} 个三元组 × 6 种顺序全部收敛并匹配 oracle"
                if not failures else f"前 3 个失败: {failures[:3]}"),
    )
    assert not failures, failures[:3]
    assert checked == len(triples)


def _stamped_components(raw_ops, cid):
    from otbackend.textmodel import Comp
    out = []
    for d in raw_ops:
        if d["type"] == "ins":
            out.append(Comp.ins(d["pos"], d["text"], cid, 1))
        else:
            out.append(Comp.del_(d["pos"], d["length"]))
    return out
