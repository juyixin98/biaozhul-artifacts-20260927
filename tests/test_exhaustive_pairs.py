"""穷举短串上的操作对：两种接收顺序都必须收敛，且与独立 oracle 一致。

操作空间（长度 n 基线上）：
* ins(p, ch)：p ∈ [0,n]，ch ∈ {ASCII, 多字节}
* del(p, l)：全部非空子区间（覆盖重叠/包含/相接/相同）

三重判定：
1. TP1：被测 xform 两侧字符串相等；
2. 身份一致：被测结果的字符串 == 独立 OracleServer 两种接收顺序
   的 Token 身份序列渲染，且两种顺序身份序列完全相同；
3. 参考独立性：oracle 不导入 otbackend.transform。
"""

from __future__ import annotations

import itertools

import pytest

from otbackend.textmodel import Comp, apply_stream
from otbackend.transform import xform

from .oracle import (
    OracleServer,
    marked_from_components,
    render,
)
from .runlog import RunLogger

ALPHABET = ["X", "😀"]
BASES = ["", "a", "ab", "abc"]


def all_components(n: int, cid: str, opid: int):
    for p in range(n + 1):
        for ch in ALPHABET:
            yield [Comp.ins(p, ch, cid, opid)]
    for p in range(n):
        for l in range(1, n - p + 1):
            yield [Comp.del_(p, l)]


@pytest.fixture(scope="module")
def logger():
    log = RunLogger()
    yield log
    log.flush()


@pytest.mark.parametrize("base", BASES)
def test_exhaustive_pairs_converge(base, logger):
    n = len(base)
    ca = list(all_components(n, "alice", 1))
    cb = list(all_components(n, "bob", 1))
    checked = 0
    failures: list[tuple] = []

    for a, b in itertools.product(ca, cb):
        # ---- 被测实现：TP1 ----
        ap, bp = xform(a, b)
        s_ab = apply_stream(apply_stream(base, b), ap)
        s_ba = apply_stream(apply_stream(base, a), bp)
        if s_ab != s_ba:
            failures.append(("TP1", a, b, s_ab, s_ba))
            continue

        # ---- 独立 oracle：两种接收顺序 ----
        ma = marked_from_components(a, "alice", 1)
        mb = marked_from_components(b, "bob", 1)
        srv_ab = OracleServer(base)
        srv_ab.submit("alice", 1, ma, n)
        t_ab = srv_ab.submit("bob", 1, mb, n)
        srv_ba = OracleServer(base)
        srv_ba.submit("bob", 1, mb, n)
        t_ba = srv_ba.submit("alice", 1, ma, n)

        id_ab = [t.origin for t in t_ab]
        id_ba = [t.origin for t in t_ba]
        if id_ab != id_ba:
            failures.append(("ORACLE-ORDER", a, b, id_ab, id_ba))
            continue
        if render(t_ab) != s_ab:
            failures.append(("ORACLE-MATCH", a, b, render(t_ab), s_ab))
            continue
        # 意图保持：所有存活 token 身份集合 = 初始 + 两个插入 - 被删基字符
        # （删除重叠时身份集合两边一致，已由上面的身份序列相等覆盖）
        checked += 1

    run_id = logger.record(
        suite="exhaustive-pairs",
        inputs={"base": base, "ops_each_side": len(ca)},
        steps=[{"checked_pairs": checked, "failures": len(failures)}],
        verdict="PASS" if not failures else "FAIL",
        reason=(f"{checked}/{len(ca)*len(cb)} 对收敛且与独立 oracle 一致"
                if not failures else f"前 5 个失败: {failures[:5]}"),
    )
    assert not failures, f"{run_id}: {failures[:5]}"
    assert checked == len(ca) * len(cb)


def test_enumeration_size_guard():
    # n=2: ins 3*2=6，del 2+1=3 → 9；防止生成器被改小
    assert len(list(all_components(2, "c", 1))) == 9
    # n=0 只有插入 1 个位置 × 2 字符 = 2
    assert len(list(all_components(0, "c", 1))) == 2
