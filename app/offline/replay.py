"""确定性离线回放。

输入：服务端导出的 JSONL 流水 + 受信 Ed25519 公钥（公钥是带外信任，绝不从
数据库读取——否则篡改者换个签名密钥即可自证）。

回放做四件互相独立的事：
1. 用内核 SMT 从空根重放规范化流水，逐版本重算根；
2. 逐版本验证检查点签名（版本、根、parent_root、batch_id 全部入签名消息）；
3. 验证版本链 parent_root 连续；
4. 若提供 SQLite 活动库，额外对 key_index 中每个存活键现取现验，
   抓出“流水没问题但活动库节点/索引被直接篡改”的情况。

任何一项失败都给出带版本号/根指纹的明确诊断。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.coding.keys import normalize_key
from app.coding.params import TreeParams
from app.coding.signing import (
    public_key_from_pem,
    verify_checkpoint,
)
from app.core.store import InMemoryStore
from app.core.smt import SparseMerkleTree
from app.diagnostics import Decision, Reason
from app.offline.verifier import verify_envelope

GENESIS_BATCH_ID = "genesis-empty-root"


@dataclass
class JournalEntry:
    version: int
    batch_id: str
    seq: int
    key: bytes
    value: bytes | None  # None=删除


@dataclass
class ReplayReport:
    decision: Decision
    reason: Reason
    message: str
    versions_checked: int = 0
    final_root: str | None = None
    failures: list[dict] = field(default_factory=list)
    live_proofs_checked: int = 0

    def as_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "reason": self.reason.value,
            "message": self.message,
            "versions_checked": self.versions_checked,
            "final_root": self.final_root,
            "live_proofs_checked": self.live_proofs_checked,
            "failures": self.failures,
        }


def load_jsonl(path: str | Path) -> list[dict]:
    """加载 JSONL。每行要么是检查点版本行，要么是 journal 操作行。"""
    records: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL 第 {lineno} 行非法 JSON: {exc}") from exc
    return records


def replay_jsonl(
    records: list[dict],
    public_key_pem: bytes,
    params: TreeParams | None = None,
) -> ReplayReport:
    """对导出的 JSONL 做独立回放。记录格式见 scripts/export_journal.py。"""
    try:
        pub = public_key_from_pem(public_key_pem)
    except Exception as exc:
        return ReplayReport(
            Decision.INCONCLUSIVE, Reason.PUBLIC_KEY_UNAVAILABLE,
            f"受信公钥无法加载（不应信任库内公钥）: {exc}",
        )

    params = params or TreeParams(32, 256)
    checkpoints: dict[int, dict] = {}
    ops_by_version: dict[int, list[dict]] = {}
    for rec in records:
        if rec.get("record") == "checkpoint":
            checkpoints[int(rec["version"])] = rec
        elif rec.get("record") == "journal":
            ops_by_version.setdefault(int(rec["version"]), []).append(rec)

    failures: list[dict] = []
    if 0 not in checkpoints:
        return ReplayReport(
            Decision.INCONCLUSIVE, Reason.ENVELOPE_MALFORMED,
            "流水缺少 v0 检查点，无法锚定空根",
        )

    # v0 必须是空根
    root = params.empty_root
    cp0 = checkpoints[0]
    if bytes.fromhex(cp0["root"]) != root:
        failures.append({"version": 0, "reason": Reason.ROOT_MISMATCH.value,
                         "detail": "v0 根不是空树根"})
    if not _check_signature(pub, cp0):
        failures.append({"version": 0, "reason": Reason.SIGNATURE_INVALID.value,
                         "detail": "v0 检查点签名错误"})

    max_version = max(checkpoints)
    store = InMemoryStore()
    tree = SparseMerkleTree(store, params)

    for version in range(1, max_version + 1):
        cp = checkpoints.get(version)
        if cp is None:
            failures.append({"version": version, "reason": Reason.ENVELOPE_MALFORMED.value,
                             "detail": "版本链缺检查点"})
            break
        claimed_root = bytes.fromhex(cp["root"])
        claimed_parent = bytes.fromhex(cp["parent_root"]) if cp.get("parent_root") else None

        if claimed_parent != root:
            failures.append({
                "version": version,
                "reason": Reason.PARENT_ROOT_MISMATCH.value,
                "detail": {
                    "claimed_parent": claimed_parent.hex() if claimed_parent else None,
                    "recomputed_parent": root.hex(),
                },
            })

        if not _check_signature(pub, cp):
            failures.append({"version": version,
                             "reason": Reason.SIGNATURE_INVALID.value,
                             "detail": "检查点签名错误"})

        # 独立规范化：按 key 排序、后者覆盖（流水本身已规范化，重放时不相信其顺序，
        # 但 JSONL 每版操作已按 seq 给出；为稳妥起见再排序一次）
        ops = sorted(ops_by_version.get(version, []), key=lambda r: (r["seq"],))
        for op in ops:
            key = normalize_key(bytes.fromhex(op["key"]), params.key_len)
            value = None if op["value"] is None else bytes.fromhex(op["value"])
            root = tree.update(root, key, value)

        if root != claimed_root:
            failures.append({
                "version": version,
                "reason": Reason.ROOT_MISMATCH.value,
                "detail": {
                    "claimed_root": claimed_root.hex(),
                    "recomputed_root": root.hex(),
                },
            })

    if failures:
        decision = Decision.REJECT
        reason = Reason.ROOT_MISMATCH
        if all(f["reason"] in (Reason.SIGNATURE_INVALID.value,
                               Reason.PUBLIC_KEY_UNAVAILABLE.value)
               for f in failures):
            reason = Reason.SIGNATURE_INVALID
        return ReplayReport(
            decision, reason,
            f"回放发现 {len(failures)} 处不一致，版本链不可信",
            versions_checked=max_version, final_root=root.hex(), failures=failures,
        )

    return ReplayReport(
        Decision.ACCEPT, Reason.REPLAY_VERIFIED,
        f"全部 {max_version} 个版本根、签名与父子链核验一致",
        versions_checked=max_version, final_root=root.hex(),
    )


def cross_check_live_store(report: ReplayReport, tree: SparseMerkleTree,
                           live_keys: list[tuple[bytes, bytes]],
                           root: bytes) -> ReplayReport:
    """对活动库的存活键索引逐条出证并用**独立验证器**核验。"""
    failures = list(report.failures)
    checked = 0
    for key, value in live_keys:
        envelope = _envelope_from_tree(tree, root, key)
        v = verify_envelope(envelope, expect_membership=True, expect_value=value)
        checked += 1
        if not v.accepted:
            failures.append({
                "reason": v.reason.value,
                "detail": {"key_fingerprint": v.key_fingerprint, "message": v.message},
            })
    if failures and report.decision is Decision.ACCEPT:
        return ReplayReport(
            Decision.REJECT, Reason.ROOT_MISMATCH,
            "活动库交叉核验发现篡改（流水一致但当前状态不可信）",
            versions_checked=report.versions_checked, final_root=report.final_root,
            failures=failures, live_proofs_checked=checked,
        )
    report.live_proofs_checked = checked
    report.failures = failures
    return report


def _envelope_from_tree(tree: SparseMerkleTree, root: bytes, key: bytes) -> dict:
    from app.api.proof_envelope import proof_to_envelope
    return proof_to_envelope(tree.prove(root, key), tree.params)


def _check_signature(pub, cp: dict) -> bool:
    return verify_checkpoint(
        pub,
        int(cp["version"]),
        cp["root"],
        cp.get("parent_root"),
        cp["batch_id"],
        bytes.fromhex(cp["signature"]),
    )
