"""交易签名摘要：把签名绑定到交易摘要与域标签。

sig_digest = SHA256(canonical_json({
    "domain_tag": <配置中的域标签>,
    "version": ..., "locktime": ...,
    "inputs":  [{txid, vout, prev_script}, ...],
    "outputs": [{value, script}, ...],
}))

- prev_script：被花费 UTXO 的锁定脚本（hex），使签名绑定“正在花费什么条件”。
- 刻意**不包含** unlock（解锁脚本本身携带签名；若入摘要会形成自引用循环）。
  txid 仍覆盖完整交易（含 unlock），因此改 unlock 会改 txid，使后续引用失效。
- 域标签错误（错误交易域）或交易任何被签字段被改动 → 摘要不同 → SIG_INVALID。
"""
from __future__ import annotations

import hashlib

from .transaction import Transaction, canonical_json


def sighash_document(tx: Transaction, prev_scripts: list[bytes], domain_tag: str) -> dict:
    if len(prev_scripts) != len(tx.inputs):
        raise ValueError("prev_scripts 数量必须与 inputs 一致")
    return {
        "domain_tag": domain_tag,
        "version": tx.version,
        "locktime": tx.locktime,
        "inputs": [
            {
                "txid": inp.txid,
                "vout": inp.vout,
                "prev_script": prev.hex(),
            }
            for inp, prev in zip(tx.inputs, prev_scripts)
        ],
        "outputs": [{"value": out.value, "script": out.script} for out in tx.outputs],
    }


def signature_digest(tx: Transaction, prev_scripts, domain_tag: str) -> bytes:
    doc = sighash_document(tx, list(prev_scripts), domain_tag)
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).digest()
