"""离线回放与黄金期望测试。

这些测试驱动的是 fixtures/scenarios 下的**人工夹具 + 人工黄金期望**；
另含一个独立预言机交叉核验：用第三方 ``rlp`` 库重新解码落库的原始字节，
确认存储的 raw 与我们的手写编解码一致。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from localtxpool.config import Config
from localtxpool.replay.runner import Replayer, ReplayAbort
from localtxpool.replay.golden import verify, GoldenMismatch

SCEN = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios"
GOLD = Path(__file__).resolve().parents[1] / "fixtures" / "golden"


def _run(name):
    r = Replayer(Config())
    r.run_file(SCEN / f"{name}.jsonl")
    return r


@pytest.mark.parametrize("name", [
    "01-fee-competition",
    "02-gaps-replace-expiry",
    "03-rollback",
])
def test_golden_scenarios_pass(name):
    r = _run(name)
    report = verify(r, GOLD / f"{name}.golden.json")
    assert report["matched_expectations"] > 0
    assert report["diffs"] == []


def test_golden_mismatch_is_detected(tmp_path):
    # 篡改一个黄金期望值必须被发现（证明核对不是恒真）
    r = _run("01-fee-competition")
    import json
    golden = json.loads((GOLD / "01-fee-competition.golden.json").read_text())
    # 把第一笔候选哈希改成错误值
    for item in golden["expect"]:
        if item.get("index") == 11:
            item["result"]["transactions"][0] = "0x" + "ff" * 32
    bad = tmp_path / "bad.golden.json"
    bad.write_text(json.dumps(golden))
    with pytest.raises(GoldenMismatch):
        verify(r, bad)


def test_virtual_clock_is_monotonic():
    r = Replayer(Config(), start_time=1000)
    # 构造一个时钟倒退事件文件
    p = tmp_events = '{"at":1000,"type":"clock"}\n{"at":999,"type":"clock"}\n'
    path = tmp_path_write(p)
    with pytest.raises(ValueError):
        r.run_file(path)


def tmp_path_write(content, name="events.jsonl"):
    import tempfile, os
    d = tempfile.mkdtemp()
    p = os.path.join(d, name)
    with open(p, "w") as fh:
        fh.write(content)
    return p


def test_unknown_event_type_aborts():
    r = Replayer(Config())
    p = tmp_path_write('{"at":1,"type":"nonsense"}\n')
    with pytest.raises(ReplayAbort):
        r.run_file(p)


def test_stored_raw_decodable_by_independent_rlp():
    """独立预言机：落库 raw 用第三方 rlp 库解码，字段必须与库记录一致。"""
    rlp_lib = pytest.importorskip("rlp")
    r = _run("01-fee-competition")
    for t in r.service.repo.list_all(
            ("pending", "queued", "included", "mined", "expired", "replaced", "evicted")):
        decoded = rlp_lib.decode(t.raw)
        # 9 个字段；nonce/gas_price 与索引记录一致
        assert len(decoded) == 9
        assert int.from_bytes(decoded[0], "big") == t.nonce
        assert int.from_bytes(decoded[1], "big") == t.gas_price
        # 重新用我们的严格解码器也必须通过
        from localtxpool.encoding import decode_signed
        tx = decode_signed(t.raw, expected_chain_id=31337)
        assert "0x" + tx.hash().hex() == t.tx_hash
