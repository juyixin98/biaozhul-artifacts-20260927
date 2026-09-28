"""Application service: wires signing, store, audit and the diff engine together.

Both the CLI and the HTTP API go exclusively through this layer, so behavior and
auditing are identical across entry points.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import diff as diff_mod
from .audit import Auditor
from .config import Config
from .evidence import (
    normalize_request,
    policy_fingerprint,
    request_identity,
)
from .kernel import evaluate
from .policy import parse_policy
from .signing import load_or_create_key, verify_object
from .store import Store, StoreError
from .types import Failure, RunResult, Verdict


class ServiceError(Exception):
    def __init__(self, code: Failure, message: str, *, details: Any = None, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.http_status = http_status


class Service:
    def __init__(self, config: Config | None = None, *, store: Store | None = None,
                 auditor: Auditor | None = None):
        self.config = config or Config()
        self.store = store or Store(self.config.db_path)
        self.key = None
        try:
            self.key = load_or_create_key(self.config.key_path)
            self.store.init_signing_key(self.key)
        except StoreError:
            raise
        self.auditor = auditor or Auditor(self.store, self.key)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "Service":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _signer(self) -> Any:
        from .signing import sign_object
        return lambda payload: sign_object(self.key, payload)

    def run_diff(self, old_doc: Any, new_doc: Any, *, run_id: str | None = None) -> RunResult:
        run_id = run_id or diff_mod.new_run_id()
        self.auditor.emit("diff-requested", run_id=run_id, detail={"old_present": True, "new_present": True})
        try:
            result = diff_mod.run_diff(
                old_doc, new_doc,
                space_cap=self.config.space_cap,
                witness_limit_per_category=self.config.witness_limit_per_category,
                signer=self._signer(),
                auditor=self.auditor.as_diff_callback(),
                run_id=run_id,
            )
        except diff_mod.DiffFailure as f:
            self.store.save_failure(
                run_id, None, None, f.code, f.message, f.details,
                datetime.now(timezone.utc).isoformat(),
            )
            self.auditor.emit("run-failed", run_id=run_id,
                              detail={"code": f.code.value, "message": f.message})
            raise ServiceError(f.code, f.message, details=f.details,
                               http_status=422 if f.code is Failure.SPACE_LIMIT_EXCEEDED else 400) from f

        created = result.created_at
        self.store.upsert_policy_version(
            result.old_version_id, result.old_policy_hash, old_doc, run_id, created)
        self.store.upsert_policy_version(
            result.new_version_id, result.new_policy_hash, new_doc, run_id, created)
        self.store.save_run(result)
        self.auditor.emit("run-stored", run_id=run_id,
                          detail={"witnesses": len(result.witnesses),
                                  "expands": result.expands,
                                  "possibly_expands": result.possibly_expands})
        return result

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self.store.get_run(run_id)
        if row is None:
            raise ServiceError(Failure.NOT_FOUND, f"run {run_id!r} not found", http_status=404)
        if row["status"] == "complete":
            import json
            return {"status": "complete", "result": json.loads(row["result_json"])}
        import json
        return {
            "status": "failed",
            "failure": {
                "code": row["failure_code"],
                "message": row["failure_message"],
                "details": json.loads(row["failure_details"] or "{}"),
            },
        }

    def list_runs(self) -> list[dict[str, Any]]:
        return self.store.list_runs()

    def get_witnesses(self, run_id: str, category: str | None = None) -> list[dict[str, Any]]:
        if self.store.get_run(run_id) is None:
            raise ServiceError(Failure.NOT_FOUND, f"run {run_id!r} not found", http_status=404)
        return self.store.get_witnesses(run_id, category)

    def get_audit(self, run_id: str | None = None, request_id: str | None = None) -> list[dict[str, Any]]:
        return self.auditor.events(run_id=run_id, request_id=request_id)

    def verify_audit_chain(self) -> dict[str, Any]:
        try:
            return self.auditor.verify_chain()
        except Exception as e:
            code = getattr(e, "code", Failure.BAD_SIGNATURE)
            raise ServiceError(code, str(e), details=getattr(e, "findings", None),
                               http_status=409) from e

    def verify_request(self, doc: Any, request_raw: Any, *, expected_verdict: str | None = None,
                       use_versions: tuple[str, str] | None = None) -> dict[str, Any]:
        """Independent re-check of one concrete request under one policy.

        ``use_versions`` resolves a stored policy by version id (fingerprint-bound),
        which makes tampering with the supplied doc detectable.
        """
        rid = "req_" + request_identity(
            {"probe": True, "doc": _doc_or_version(doc, use_versions, 0), "request": request_raw}
        )[:16]
        self.auditor.emit("verify-requested", request_id=rid)
        policy = self._resolve_policy(doc, use_versions, 0)
        try:
            request = normalize_request(request_raw)
        except ValueError as e:
            self.auditor.emit("verify-invalid-request", request_id=rid, detail={"reason": str(e)})
            raise ServiceError(Failure.INVALID_REQUEST, str(e), http_status=400) from e

        decision = evaluate(policy, request)
        verdict = decision.verdict
        consistent = True
        if expected_verdict is not None:
            try:
                wanted = Verdict(expected_verdict)
            except ValueError as e:
                raise ServiceError(Failure.INVALID_REQUEST, f"unknown verdict {expected_verdict!r}",
                                   http_status=400) from e
            consistent = verdict is wanted
            if not consistent:
                self.auditor.emit("evidence-mismatch", request_id=rid,
                                  detail={"expected": wanted.value, "actual": verdict.value})
        self.auditor.emit("verify-complete", request_id=rid, detail={"verdict": verdict.value})
        return {
            "request_id": rid,
            "request": {k: request[k] for k in ("principal", "action", "resource")} | {
                "attributes": _jsonable_attrs(request["attributes"])},
            "verdict": verdict.value,
            "reason": decision.reason,
            "trace": decision.trace,
            "policy_fingerprint": policy_fingerprint(policy.raw),
            "expected": expected_verdict,
            "consistent": consistent,
        }

    def verify_run_signature(self, run_id: str) -> dict[str, Any]:
        import json
        row = self.store.get_run(run_id)
        if row is None:
            raise ServiceError(Failure.NOT_FOUND, f"run {run_id!r} not found", http_status=404)
        if row["status"] != "complete":
            return {"run_id": run_id, "valid": False, "reason": "run did not complete"}
        result = json.loads(row["result_json"])
        signature = result.pop("signature", None)
        if not signature:
            return {"run_id": run_id, "valid": False, "reason": "no signature present"}
        valid = verify_object(self.store.public_key(), result, signature)
        if not valid:
            self.auditor.emit("run-signature-invalid", run_id=run_id)
        return {"run_id": run_id, "valid": valid, "reason": "ok" if valid else "signature mismatch"}

    def _resolve_policy(self, doc: Any, use_versions: tuple[str, str] | None, idx: int) -> Any:
        if use_versions is not None and use_versions[idx]:
            row = self.store.get_policy_version(use_versions[idx])
            if row is None:
                raise ServiceError(Failure.NOT_FOUND, f"version {use_versions[idx]!r} not found",
                                   http_status=404)
            import json
            stored_doc = json.loads(row["document"])
            if doc is not None and policy_fingerprint(doc) != row["fingerprint"]:
                raise ServiceError(
                    Failure.EVIDENCE_MISMATCH,
                    f"supplied document does not match stored version {use_versions[idx]!r}",
                    http_status=409,
                )
            doc = stored_doc
        if doc is None:
            raise ServiceError(Failure.INVALID_REQUEST, "a policy document or stored version is required")
        return parse_policy(doc)


def _doc_or_version(doc: Any, versions: tuple[str, str] | None, idx: int) -> Any:
    return versions[idx] if versions is not None and versions[idx] else doc


def _jsonable_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    from .evidence import request_to_jsonable
    return request_to_jsonable({"attributes": attrs})["attributes"]
