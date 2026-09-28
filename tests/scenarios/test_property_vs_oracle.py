"""属性测试：随机操作流下，服务端与独立预言机逐行结论必须一致。

随机流覆盖：多文件、重复键、NULL 键、先删后插、重写、行号删除、
跨文件等值删除。预言机独立实现规则，是本测试的唯一断言来源
（不使用被测代码生成期望值）。
"""
from __future__ import annotations

import random

import pytest

from conftest import snapshot as http_snapshot, verdict_index
from oracle.reference import KERNEL_EQUIVALENCE, ReferenceTable

KEY_SPACE = [1, 2, 3, None]  # 含 NULL，刻意制造重复键
NAMES = ["x", "y", None]


def _rand_rows(rng: random.Random, n: int) -> list[dict]:
    return [{"id": rng.choice(KEY_SPACE), "name": rng.choice(NAMES),
             "age": rng.randint(0, 9)} for _ in range(n)]


@pytest.mark.scenario
@pytest.mark.parametrize("seed", list(range(30)))
def test_random_flow_matches_oracle(client, seed):
    rng = random.Random(seed * 7919 + 13)
    table = f"rand{seed}"
    r = client.post("/tables", json={
        "table_id": table,
        "columns": {"id": "int64", "name": "string", "age": "int64"},
        "key": ["id"],
    })
    assert r.status_code == 201

    oracle = ReferenceTable(["id"])
    live_files: list[str] = []
    op_counter = 0

    def new_id(prefix: str) -> str:
        nonlocal op_counter
        op_counter += 1
        return f"{prefix}-{seed}-{op_counter}"

    def compare():
        snap = http_snapshot(client, table)
        o_verdicts = oracle.scan_verdicts()
        idx = verdict_index(snap)
        assert len(o_verdicts) == len(idx), (seed, snap, o_verdicts)
        for ov in o_verdicts:
            sv = idx[(ov["file_id"], ov["row_number"])]
            assert sv["action"] == ov["action"], (seed, ov, sv)
            assert sv["reason"] == KERNEL_EQUIVALENCE[ov["oracle_reason"]], (seed, ov, sv)
            assert sv["by_delete_id"] == ov["by_delete_id"], (seed, ov, sv)
            assert sv["by_seq"] == ov["by_seq"], (seed, ov, sv)
            assert sv["insert_seq"] == ov["insert_seq"], (seed, ov, sv)
            assert sv["values"]["id"] == ov["values"]["id"], (seed, ov, sv)
        # 操作级状态映射
        statuses = oracle.op_statuses()
        for e in snap["op_evaluations"]:
            want = statuses[e["delete_id"]]
            assert e["status"] == KERNEL_EQUIVALENCE[want], (seed, e, want)

    for cycle in range(6):
        # 1) 载入 1~2 个文件
        for _ in range(rng.randint(1, 2)):
            fid = new_id("file")
            rows = _rand_rows(rng, rng.randint(1, 5))
            r = client.post(f"/tables/{table}/load", json={
                "file_id": fid, "source": {"kind": "inline", "rows": rows},
            })
            assert r.status_code == 201, r.text
            oracle.load(fid, rows)
            live_files.append(fid)

        # 2) 若干等值删除（含 NULL 谓词）
        for _ in range(rng.randint(0, 3)):
            did = new_id("eq")
            kval = rng.choice(KEY_SPACE)
            r = client.post(f"/tables/{table}/deletes", json={"deletes": [{
                "delete_id": did, "kind": "equality", "key": {"id": kval},
            }]})
            assert r.status_code == 200, r.text
            seq = r.json()["result"]["results"][0]["seq"]
            oseq = oracle.equality_delete(did, [kval])
            assert seq == oseq

        # 3) 一个位置删除（针对随机 live 文件的随机行号）
        if live_files and rng.random() < 0.8:
            fid = rng.choice(live_files)
            snap = http_snapshot(client, table)
            fmeta = next(f for f in snap["files"] if f["file_id"] == fid and f["is_live"])
            rn = rng.randrange(fmeta["row_count"])
            did = new_id("pos")
            r = client.post(f"/tables/{table}/deletes", json={"deletes": [{
                "delete_id": did, "kind": "position",
                "file_id": fid, "row_number": rn,
            }]})
            if r.status_code == 200:
                seq = r.json()["result"]["results"][0]["seq"]
                try:
                    oseq = oracle.position_delete(did, fid, rn)
                except ValueError:
                    # 服务端接受但预言机判定非法（不应发生），立即失败暴露分歧
                    raise AssertionError(
                        f"seed={seed} 服务端接受了预言机拒绝的位置删除 {did}")
                assert seq == oseq
            else:
                # 服务端拒绝（该文件已被重写、不再 live）：预言机也必须拒绝，且不发号
                assert r.json()["error"]["category"] == "state_conflict"
                try:
                    oracle.position_delete(did, fid, rn)
                except ValueError:
                    pass
                else:
                    raise AssertionError(
                        f"seed={seed} 服务端拒绝了但预言机接受位置删除 {did}")
                live_files = [f for f in live_files if f != fid]
        compare()

        # 4) 可能重写：随机取若干 live 文件合并
        candidates = [f for f in live_files]
        if candidates and rng.random() < 0.6:
            k = rng.randint(1, min(2, len(candidates)))
            parents = rng.sample(candidates, k)
            snap = http_snapshot(client, table)
            # 全部幸存行为空时服务拒绝；预言机也拒绝，跳过即可
            new_fid = new_id("rew")
            r = client.post(f"/tables/{table}/rewrite", json={
                "file_ids": parents, "new_file_id": new_fid,
            })
            if r.status_code == 200:
                oracle.rewrite(parents, new_fid)
                live_files = [f for f in live_files if f not in parents] + [new_fid]
            else:
                assert r.status_code == 409
                assert r.json()["error"]["category"] == "state_conflict"
        compare()

    compare()
