"""端到端本地演示：合成两版策略 + 历史证据，跑一次差分并打印可解释结果。

运行：.venv/bin/python scripts/demo.py
全程使用临时 SQLite 与 fixtures 下的本地密钥，不访问任何外部服务。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from diffanalyzer.audit import Auditor
from diffanalyzer.config import load_config
from diffanalyzer.crypto_verify import KeyRegistry
from diffanalyzer.local_signing import signed_evidence_envelope, signed_policy_envelope
from diffanalyzer.service import PolicyService
from diffanalyzer.store import Store


def main() -> None:
    cfg = load_config()
    priv = cfg.abs_path(cfg.demo_private_key_path).read_bytes()
    pub = cfg.abs_path(cfg.trusted_key_path).read_bytes()

    tmpdir = tempfile.mkdtemp(prefix="poldiff_demo_")
    store = Store(str(Path(tmpdir) / "demo.db"))
    auditor = Auditor(store)
    registry = KeyRegistry.from_pems({"submitter": pub.decode()})
    svc = PolicyService(cfg, store, auditor, registry)

    # 注意：不写兜底 deny-all。默认拒绝由内核提供；若写一条 actions:["*"] 的
    # DENY，则在“显式拒绝优先”语义下所有 ALLOW 都会失效。
    old_doc = {
        "version": "v1",
        "rules": [
            {"id": "read-logs", "effect": "ALLOW", "resource_prefix": "logs/",
             "actions": ["s3:GetObject"], "principals": ["acct/alice"],
             "conditions": [
                 {"attribute": "tls", "op": "Eq", "value": True},
                 {"attribute": "ip", "op": "CidrMatch", "value": "10.0.0.0/8"},
             ]},
        ],
    }
    # v2：新增一条对 logs-secret/ 的 ALLOW（边界：不能只因 logs/ 前缀而放行），
    # 且在 tmp/ 上用 NotEq 条件（未知值时必须 UNKNOWN）。
    new_doc = {
        "version": "v2",
        "rules": [
            {"id": "read-logs", "effect": "ALLOW", "resource_prefix": "logs/",
             "actions": ["s3:GetObject"], "principals": ["acct/alice"],
             "conditions": [
                 {"attribute": "tls", "op": "Eq", "value": True},
                 {"attribute": "ip", "op": "CidrMatch", "value": "10.0.0.0/8"},
             ]},
            {"id": "read-secret", "effect": "ALLOW",
             "resource_prefix": "logs-secret/",
             "actions": ["s3:GetObject"], "principals": ["acct/bob"]},
            {"id": "write-tmp", "effect": "ALLOW", "resource_prefix": "tmp/",
             "actions": ["s3:PutObject"], "principals": ["*"],
             "anonymous": True,
             "conditions": [
                 {"attribute": "retention_locked", "op": "NotEq", "value": True},
             ]},
        ],
    }

    rid = auditor.new_request_id()
    svc.submit_policy(signed_policy_envelope(priv, old_doc),
                      request_id=rid, actor="demo-operator")
    svc.submit_policy(signed_policy_envelope(priv, new_doc),
                      request_id=rid, actor="demo-operator")

    evidence_env = signed_evidence_envelope(
        priv,
        bundle_id="ev-demo-1",
        policy_version="v1",
        scope_prefixes=["logs/"],
        scope_actions=["s3:GetObject"],
        records=[
            {"principal": "acct/alice", "action": "s3:GetObject",
             "resource": "logs/2026/a.log",
             "attributes": {"tls": True, "ip": "10.1.2.3"},
             "observed": "ALLOW", "observed_by_rule": "read-logs"},
            {"principal": "acct/bob", "action": "s3:GetObject",
             "resource": "logs/2026/b.log",
             "attributes": {"tls": True, "ip": "10.1.2.3"},
             "observed": "DENY", "observed_by_rule": None},
        ],
    )
    svc.submit_evidence(evidence_env, request_id=rid, actor="demo-operator")

    result = svc.run_diff(
        old_version="v1",
        new_version="v2",
        scope={"resource_prefixes": ["logs/", "logs-secret/", "tmp/"],
               "actions": ["s3:GetObject", "s3:PutObject"]},
        request_id=rid,
        actor="demo-operator",
        evidence_bundle_id="ev-demo-1",
    )

    out = {
        "diff_id": result.diff_id,
        "request_id": rid,
        "summary": result.summary,
        "space_regions": result.space["regions"],
        "widened_witness": result.witnesses["widened"][:2],
        "new_unknown_witness": result.witnesses["new_unknown"][:2],
        "evidence_report": result.evidence_report,
        "audit": auditor.query(request_id=rid, limit=50)[:6],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    store.close()


if __name__ == "__main__":
    main()
