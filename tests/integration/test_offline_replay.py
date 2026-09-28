"""集成：离线回放——导出流水、独立重算、签名链、活动库篡改检测。"""
from __future__ import annotations

import json


from app.coding.params import TreeParams
from app.coding.signing import (
    generate_private_key,
    private_key_from_hex,
    public_key_to_pem,
)
from app.diagnostics import Decision, Reason
from app.offline.replay import (
    cross_check_live_store,
    load_jsonl,
    replay_jsonl,
)
from app.core.smt import SparseMerkleTree
from tests.integration.test_api_flow import batch, k


def _export_records(client):
    store = client.app.state.service.store
    records: list[dict] = []
    for cp in store.all_checkpoints():
        records.append({
            "record": "checkpoint",
            "version": cp["version"],
            "root": cp["root"].hex(),
            "parent_root": cp["parent_root"].hex() if cp["parent_root"] else None,
            "batch_id": cp["batch_id"],
            "signature": cp["signature"].hex(),
        })
    for row in store.all_journal():
        records.append({
            "record": "journal",
            "version": row["version"],
            "batch_id": row["batch_id"],
            "seq": row["seq"],
            "key": row["nkey"].hex(),
            "value": None if row["nvalue"] is None else row["nvalue"].hex(),
        })
    return records


def test_replay_accepts_clean_history(client, public_key_pem):
    batch(client, [(k(1), b"a"), (k(2), b"b")], idem="r1")
    batch(client, [(k(3), b"c"), (k(1), None)], idem="r2")  # 含删除
    batch(client, [(k(1), b"restored")], idem="r3")         # 删除复原

    records = _export_records(client)
    report = replay_jsonl(records, public_key_pem, TreeParams(32, 256))
    assert report.decision is Decision.ACCEPT, report.failures
    assert report.reason is Reason.REPLAY_VERIFIED
    assert report.versions_checked == 3
    # 重放最终根 == 服务当前根
    assert report.final_root == client.get("/root").json()["root"]


def test_replay_detects_tampered_journal_root(client, public_key_pem):
    batch(client, [(k(1), b"a"), (k(2), b"b")])
    records = _export_records(client)

    # 篡改 v1 流水：把值改了，但检查点签名仍对应旧根 -> 重算根不符
    for rec in records:
        if rec.get("record") == "journal" and rec["version"] == 1:
            rec["value"] = "deadbeef"
    report = replay_jsonl(records, public_key_pem, TreeParams(32, 256))
    assert report.decision is Decision.REJECT
    assert report.failures
    assert any(f["reason"] == "ROOT_MISMATCH" and f["version"] == 1
               for f in report.failures)


def test_replay_detects_forged_signature(client, settings):
    batch(client, [(k(1), b"a")])
    records = _export_records(client)
    # 攻击者用自己的密钥重签 v1（但根也改了）
    attacker = generate_private_key()
    for rec in records:
        if rec.get("record") == "checkpoint" and rec["version"] == 1:
            rec["root"] = "00" * 32
            from app.coding.signing import sign_checkpoint
            rec["signature"] = sign_checkpoint(
                attacker, 1, rec["root"], rec["parent_root"], rec["batch_id"]
            ).hex()
    # 受信公钥（原服务密钥）必须拒绝
    trusted = public_key_to_pem(
        private_key_from_hex(settings.signing_key_hex).public_key()
    )
    report = replay_jsonl(records, trusted, TreeParams(32, 256))
    assert report.decision is Decision.REJECT
    reasons = {f["reason"] for f in report.failures}
    assert Reason.SIGNATURE_INVALID.value in reasons
    assert Reason.ROOT_MISMATCH.value in reasons


def test_replay_rejects_wrong_public_key(client, public_key_pem):
    batch(client, [(k(1), b"a")])
    records = _export_records(client)
    wrong_key_pem = public_key_to_pem(generate_private_key().public_key())
    report = replay_jsonl(records, wrong_key_pem, TreeParams(32, 256))
    assert report.decision is Decision.REJECT
    assert any(f["reason"] == "SIGNATURE_INVALID" for f in report.failures)


def test_replay_parent_chain_break_detected(client, public_key_pem):
    batch(client, [(k(1), b"a")], idem="c1")
    batch(client, [(k(2), b"b")], idem="c2")
    records = _export_records(client)
    # 把 v2 的 parent_root 改掉
    for rec in records:
        if rec.get("record") == "checkpoint" and rec["version"] == 2:
            rec["parent_root"] = "11" * 32
    report = replay_jsonl(records, public_key_pem, TreeParams(32, 256))
    assert report.decision is Decision.REJECT
    assert any(f["reason"] == "PARENT_ROOT_MISMATCH" and f["version"] == 2
               for f in report.failures)


def test_cross_check_detects_live_store_value_tampering(client, public_key_pem):
    batch(client, [(k(1), b"a"), (k(2), b"b")])
    svc = client.app.state.service
    store = svc.store

    # 干净状态：交叉核验通过
    tree = SparseMerkleTree(store, svc.params)
    from app.offline.replay import ReplayReport
    clean = ReplayReport(Decision.ACCEPT, Reason.REPLAY_VERIFIED, "clean")
    clean = cross_check_live_store(clean, tree, store.live_keys(), svc.current_root())
    assert clean.decision is Decision.ACCEPT
    assert clean.live_proofs_checked == 2

    # 模拟活动库 key_index 被直接改成错误值（节点未动）：
    # 索引声称 k1=z，但树证明给出 a -> 值绑定失败
    store.upsert_index(bytes.fromhex(k(1)), b"z", svc.current_version())
    bad = ReplayReport(Decision.ACCEPT, Reason.REPLAY_VERIFIED, "pre")
    bad = cross_check_live_store(bad, tree, store.live_keys(), svc.current_root())
    assert bad.decision is Decision.REJECT
    assert bad.live_proofs_checked >= 1
    assert any(f["reason"] in ("VALUE_MISMATCH", "ROOT_MISMATCH") for f in bad.failures)


def test_jsonl_roundtrip_and_cli_records(client, tmp_path, public_key_pem):
    batch(client, [(k(1), b"a")])
    path = tmp_path / "export.jsonl"
    records = _export_records(client)
    path.write_text("\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n")
    loaded = load_jsonl(path)
    report = replay_jsonl(loaded, public_key_pem, TreeParams(32, 256))
    assert report.decision is Decision.ACCEPT


def test_replay_cli_exit_codes(client, tmp_path, public_key_pem):
    """CLI：干净流水退出 0；篡改流水退出 2。"""
    from app.offline.replay_cli import main

    batch(client, [(k(1), b"a")])
    good = tmp_path / "good.jsonl"
    good.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in _export_records(client)) + "\n"
    )
    pub = tmp_path / "pub.pem"
    pub.write_bytes(public_key_pem)

    assert main(["--journal", str(good), "--public-key", str(pub)]) == 0

    bad = tmp_path / "bad.jsonl"
    records = _export_records(client)
    for rec in records:
        if rec.get("record") == "journal":
            rec["value"] = "cafe"
    bad.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n"
    )
    assert main(["--journal", str(bad), "--public-key", str(pub)]) == 2
