"""Audit orchestration: parsing -> limits -> kernel -> signed report.

The service is the only module that touches both the kernel and the
store. Reports are signed with a local Ed25519 key (no external PKI), so
a stored report can be verified offline. Per-run cache-key secrets are
derived from a local master secret with HKDF and never written to disk:
keys are reproducible for replay but raw credentials cannot be recovered
from the database.
"""
from __future__ import annotations

import base64
import json
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .errors import ComputationError, ResourceExhaustedError
from .kernel import Kernel
from .models import CachePolicy
from .parsing import load_fixture, parse_evidence, parse_policy
from .store import SQLiteStore


def canonical_json(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class AuditService:
    def __init__(
        self,
        db_path: str | Path,
        *,
        fixtures_dir: str | Path | None = None,
        key_dir: str | Path | None = None,
    ):
        self.store = SQLiteStore(db_path)
        self.fixtures_dir = Path(fixtures_dir) if fixtures_dir else None
        key_dir = Path(key_dir) if key_dir else (Path(db_path).parent if db_path != ":memory:" else Path("."))
        if db_path == ":memory:":
            # Ephemeral service: generate throwaway material in memory.
            self._signing_key = Ed25519PrivateKey.generate()
            self._master_secret = secrets.token_bytes(32)
        else:
            key_dir.mkdir(parents=True, exist_ok=True)
            self._signing_key = self._load_or_create_signing_key(key_dir / "audit_signing_key.pem")
            self._master_secret = self._load_or_create_master_secret(
                key_dir / "audit_master.key"
            )
        digest = hashes.Hash(hashes.SHA256())
        digest.update(self._signing_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ))
        self.key_id = "ed25519-" + digest.finalize().hex()[:16]

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------
    # local key material
    # ------------------------------------------------------------------
    @staticmethod
    def _load_or_create_signing_key(path: Path) -> Ed25519PrivateKey:
        if path.exists():
            return serialization.load_pem_private_key(path.read_bytes(), password=None)  # type: ignore[return-value]
        key = Ed25519PrivateKey.generate()
        path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        return key

    @staticmethod
    def _load_or_create_master_secret(path: Path) -> bytes:
        if path.exists():
            return path.read_bytes()
        secret = secrets.token_bytes(32)
        path.write_bytes(secret)
        return secret

    def _run_secret(self, run_id: str) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"cache-key-vary-audit",
            info=f"run:{run_id}".encode(),
        ).derive(self._master_secret)

    # ------------------------------------------------------------------
    def _resolve_evidence(
        self,
        evidence_doc: dict[str, Any] | None,
        fixture_name: str | None,
    ) -> dict[str, Any]:
        if evidence_doc is not None and fixture_name is not None:
            from .errors import InputError
            raise InputError(
                "provide either 'evidence' or 'fixture', not both",
                code="evidence.ambiguous_source",
            )
        if evidence_doc is not None:
            return evidence_doc
        if fixture_name is not None:
            if self.fixtures_dir is None:
                from .errors import InputError
                raise InputError(
                    "service has no fixtures_dir configured",
                    code="fixture.disabled",
                )
            safe = Path(fixture_name).name  # keep callers inside the directory
            if safe != fixture_name or "/" in fixture_name or ".." in fixture_name:
                from .errors import InputError
                raise InputError(
                    f"invalid fixture name {fixture_name!r}",
                    code="fixture.bad_name",
                    detail={"fixture": fixture_name},
                )
            return load_fixture(self.fixtures_dir / f"{safe}.json")
        from .errors import InputError
        raise InputError(
            "provide inline 'evidence' or a 'fixture' name",
            code="evidence.no_source",
        )

    def run_audit(
        self,
        policy_doc: dict[str, Any],
        *,
        evidence: dict[str, Any] | None = None,
        fixture: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Run one audit, persist it, and return the signed report."""
        policy: CachePolicy = parse_policy(policy_doc)
        evidence_doc = self._resolve_evidence(evidence, fixture)
        reqs, resps = parse_evidence(evidence_doc)

        if len(reqs) > policy.max_requests:
            raise ResourceExhaustedError(
                f"evidence has {len(reqs)} requests but the policy allows "
                f"at most {policy.max_requests}",
                code="limit.requests",
                detail={"count": len(reqs), "limit": policy.max_requests},
            )

        run_id = run_id or "run-" + uuid.uuid4().hex[:12]
        created_at = datetime.now(timezone.utc).isoformat()
        self.store.create_run(run_id, policy_doc, created_at)

        kernel = Kernel(self._run_secret(run_id))

        # Persist the inputs needed for replay, but never raw credentials:
        # Authorization/Cookie header values are replaced by the kernel's
        # HMAC identity tag before they touch the database.
        for req in reqs:
            safe_headers = {
                k: v for k, v in req.headers.items()
                if k not in ("authorization", "cookie")
            }
            self.store.save_request(
                run_id,
                req.id,
                {
                    "id": req.id,
                    "method": req.method,
                    "path": req.path,
                    "query": list(req.query),
                    "headers": safe_headers,
                    "identity": kernel.identity_tag(req),
                },
            )

        findings, events, stats = kernel.audit(policy, reqs, resps)
        self.store.save_events(run_id, events)

        severities: dict[str, int] = {}
        for f in findings:
            severities[f.severity] = severities.get(f.severity, 0) + 1

        report: dict[str, Any] = {
            "run_id": run_id,
            "created_at": created_at,
            "key_id": self.key_id,
            "policy": {
                "name": policy.name,
                "covered_dimensions": list(policy.covered_dimensions),
                "identity_mode": policy.identity_mode,
                "shared": policy.shared,
            },
            "evidence": {
                "name": evidence_doc.get("name"),
                "requests": len(reqs),
                "responses": len(resps),
                "request_ids": [r.id for r in reqs],
            },
            "stats": stats,
            "summary": {
                "findings": len(findings),
                "collisions": sum(1 for f in findings if f.kind == "collision"),
                "critical": severities.get("critical", 0),
                "warning": severities.get("warning", 0),
                "info": severities.get("info", 0),
            },
            "findings": [f.to_dict() for f in findings],
        }
        signature = base64.b64encode(
            self._signing_key.sign(canonical_json(report))
        ).decode("ascii")
        report["signature"] = signature

        try:
            self.store.finalize_run(run_id, report, signature, self.key_id)
        except Exception:
            raise
        return report

    # ------------------------------------------------------------------
    def get_report(self, run_id: str) -> dict[str, Any]:
        return self.store.get_report(run_id)

    def get_events(self, run_id: str) -> list[dict[str, Any]]:
        return self.store.get_events(run_id)

    def verify_report(self, run_id: str) -> dict[str, Any]:
        report, signature, key_id = self.store.get_signature_row(run_id)
        if key_id != self.key_id:
            raise ComputationError(
                f"report key id {key_id!r} does not match local key {self.key_id!r}",
                code="verify.key_mismatch",
                detail={"report_key_id": key_id, "local_key_id": self.key_id},
            )
        payload = {k: v for k, v in report.items() if k != "signature"}
        try:
            pub: Ed25519PublicKey = self._signing_key.public_key()
            pub.verify(base64.b64decode(signature), canonical_json(payload))
        except InvalidSignature:
            return {"run_id": run_id, "valid": False, "key_id": key_id}
        return {"run_id": run_id, "valid": True, "key_id": key_id}
