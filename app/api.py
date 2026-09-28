"""FastAPI application: redaction endpoints, streaming, audit interface.

Every response carries a ``request_id``; every meaningful step is recorded in
the hash-chained audit event log. The service's own log lines contain only
structural metadata and are scrubbed against the request's secrets, so the
service never leaks originals into its own logs.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from . import __version__
from .audit import AuditDenied, AuditStore, IntegrityError, NotFound
from .config import Settings, get_settings, new_request_id
from .kernel import redact_full
from .logging_utils import safe_log
from .rules import RuleCompileError, RuleSet, UnknownProfileError, build_ruleset
from .state import SessionError, SessionStore
from . import schemas as S


def _uncertain_dto(uncertain: list[Any]) -> list[dict[str, Any]]:
    import hashlib

    out = []
    for u in uncertain:
        out.append(
            {
                "rule_id": u.rule_id,
                "label": u.label,
                "start": u.start,
                "end": u.end,
                "length": u.length,
                "reason": u.reason,
                "original_sha256": hashlib.sha256(u.original.encode()).hexdigest(),
            }
        )
    return out


def _mapping_dto(m: Any) -> dict[str, Any]:
    return {
        "rule_id": m.rule_id,
        "label": m.label,
        "original_start": m.original_start,
        "original_end": m.original_end,
        "output_start": m.output_start,
        "output_end": m.output_end,
        "original_length": m.original_length,
        "replaced_length": m.replaced_length,
        "uncertain": m.uncertain,
        "original_sha256": m.original_sha256,
        "reason": m.reason,
    }


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="logsafe — field/pattern log redaction",
        version=__version__,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings

    from .crypto import FragmentCipher

    app.state.audit = AuditStore(settings.db_path, FragmentCipher(settings.fernet_key))
    app.state.sessions = SessionStore()
    app.state.rulesets = {
        "standard": build_ruleset("standard"),
        "strict": build_ruleset("strict"),
    }
    app.state.audit.append_event(
        "service.start",
        {"version": __version__, "profiles": list(app.state.rulesets)},
    )
    safe_log.info(
        "service.start",
        version=__version__,
        detail="local synthetic-data deployment",
    )

    # -- error helpers ----------------------------------------------------

    def error(
        request_id: str,
        category: str,
        message: str,
        status: int,
        *,
        session_id: Optional[str] = None,
    ) -> JSONResponse:
        safe_log.warning(
            "request.failed",
            banned=[],
            request_id=request_id,
            session_id=session_id,
            error_category=category,
            status_code=status,
            detail=message,
        )
        return JSONResponse(
            status_code=status,
            content={
                "request_id": request_id,
                "error_category": category,
                "message": message,
            },
        )

    def ruleset_or_error(profile: str, request_id: str):
        try:
            cached = app.state.rulesets.get(profile)
            if cached is None:
                cached = build_ruleset(profile)
                app.state.rulesets[profile] = cached
            return cached, None
        except UnknownProfileError as exc:
            return None, error(request_id, "unknown_profile", str(exc), 404)
        except RuleCompileError as exc:
            return None, error(
                request_id, "rule_compile_error", str(exc), 500
            )

    # -- middleware: correlation id + safe access logging -----------------

    @app.middleware("http")
    async def correlate(request: Request, call_next):
        client_rid = request.headers.get("x-request-id", "")
        request_id = client_rid if _valid_request_id(client_rid) else new_request_id()
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response

    # -- exception handlers ----------------------------------------------

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError):
        rid = getattr(request.state, "request_id", new_request_id())
        # Only echo field locations + error types, never submitted values.
        locs = sorted(
            {
                ".".join(str(p) for p in e.get("loc", ()) if p != "body")
                for e in exc.errors()
            }
        )
        return error(rid, "validation_error", f"invalid request fields: {locs}", 422)

    @app.exception_handler(ValidationError)
    async def _validation(request: Request, exc: ValidationError):
        rid = getattr(request.state, "request_id", new_request_id())
        # Only echo field names + error types, never submitted values.
        locs = [
            ".".join(str(p) for p in e.get("loc", []))
            for e in exc.errors()
        ]
        return error(
            rid,
            "validation_error",
            f"invalid request fields: {sorted(set(locs))}",
            422,
        )

    @app.exception_handler(SessionError)
    async def _session(request: Request, exc: SessionError):
        rid = getattr(request.state, "request_id", new_request_id())
        status_map = {"session_not_found": 404, "session_closed": 409}
        status = status_map.get(exc.category, 409)
        return error(rid, exc.category, str(exc), status)

    @app.exception_handler(AuditDenied)
    async def _denied(request: Request, exc: AuditDenied):
        rid = getattr(request.state, "request_id", new_request_id())
        app.state.audit.append_event(
            "audit.denied", {"path": str(request.url.path)}, request_id=rid
        )
        return error(rid, "audit_access_denied", "invalid audit key", 403)

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound):
        rid = getattr(request.state, "request_id", new_request_id())
        return error(rid, "not_found", f"unknown id: {exc.args[0]}", 404)

    @app.exception_handler(IntegrityError)
    async def _integrity(request: Request, exc: IntegrityError):
        rid = getattr(request.state, "request_id", new_request_id())
        return error(rid, "audit_integrity_error", str(exc), 500)

    @app.exception_handler(Exception)
    async def _internal(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", new_request_id())
        safe_log.error(
            "request.internal_error",
            request_id=rid,
            error_category="internal_error",
            detail=type(exc).__name__,
        )
        return error(rid, "internal_error", "internal error", 500)

    # -- routes -----------------------------------------------------------

    @app.get("/health")
    async def health():
        chain = app.state.audit.verify_chain()
        return {
            "status": "ok" if chain["ok"] else "tampered",
            "version": __version__,
            "audit_chain": chain,
        }

    @app.get("/api/v1/rules")
    async def list_rules():
        return {
            "profiles": [
                {
                    "profile": rs.profile,
                    "version": rs.version,
                    "fingerprint": rs.fingerprint,
                    "rule_count": len(rs.rules),
                    "rules": [
                        {
                            "rule_id": r.rule_id,
                            "kind": r.kind.value,
                            "label": r.label,
                            "priority": r.priority,
                            "validator": r.validator_name,
                            "max_len": r.max_len,
                        }
                        for r in rs.rules
                    ],
                }
                for rs in app.state.rulesets.values()
            ]
        }

    @app.post("/api/v1/redact", response_model=S.RedactResponse)
    async def redact(payload: S.RedactRequest, request: Request):
        rid = request.state.request_id
        ruleset, err = ruleset_or_error(payload.profile, rid)
        if err:
            return err
        result = redact_full(payload.text, ruleset)
        banned = [payload.text[m.original_start : m.original_end] for m in result.mappings]
        app.state.audit.record_request(
            request_id=rid,
            session_id=None,
            endpoint="/api/v1/redact",
            ruleset=ruleset,
            input_length=result.input_length,
            output_length=result.output_length,
            matches=len(result.mappings),
            uncertain=len(result.uncertain),
        )
        originals = {
            (m.original_start, m.original_end): payload.text[
                m.original_start : m.original_end
            ]
            for m in result.mappings
        }
        app.state.audit.record_fragments(
            request_id=rid,
            session_id=None,
            mappings=result.mappings,
            originals_by_key=originals,
        )
        app.state.audit.append_event(
            "redact.complete",
            {
                "input_length": result.input_length,
                "output_length": result.output_length,
                "matches": len(result.mappings),
                "uncertain": _uncertain_dto(result.uncertain),
            },
            request_id=rid,
        )
        safe_log.info(
            "redact.complete",
            banned=banned,
            request_id=rid,
            rule_profile=ruleset.profile,
            rule_version=ruleset.version,
            input_length=result.input_length,
            output_length=result.output_length,
            matches=len(result.mappings),
            uncertain=len(result.uncertain),
        )
        return S.RedactResponse(
            request_id=rid,
            rule_profile=ruleset.profile,
            rule_version=ruleset.version,
            rule_fingerprint=ruleset.fingerprint,
            redacted=result.redacted,
            mappings=[_mapping_dto(m) for m in result.mappings],
            uncertain=_uncertain_dto(result.uncertain),
            input_length=result.input_length,
            output_length=result.output_length,
        )

    @app.post("/api/v1/sessions", response_model=S.SessionOpenResponse)
    async def open_session(payload: S.RedactRequest, request: Request):
        rid = request.state.request_id
        ruleset, err = ruleset_or_error(payload.profile, rid)
        if err:
            return err
        session = app.state.sessions.create(ruleset)
        app.state.audit.append_event(
            "session.open",
            {"rule_version": ruleset.version, "rule_fingerprint": ruleset.fingerprint},
            request_id=rid,
            session_id=session.session_id,
        )
        safe_log.info(
            "session.open",
            request_id=rid,
            session_id=session.session_id,
            rule_profile=ruleset.profile,
            rule_version=ruleset.version,
        )
        return S.SessionOpenResponse(
            request_id=rid,
            session_id=session.session_id,
            rule_profile=ruleset.profile,
            rule_version=ruleset.version,
            rule_fingerprint=ruleset.fingerprint,
        )

    @app.post("/api/v1/sessions/chunk", response_model=S.StreamChunkResponse)
    async def push_chunk(payload: S.StreamChunkRequest, request: Request):
        rid = request.state.request_id
        ruleset, err = ruleset_or_error(payload.profile, rid)
        if err:
            return err
        session = app.state.sessions.get(payload.session_id)
        app.state.sessions.pin_check(session, ruleset)
        if session.finished:
            raise SessionError("session_closed", "session already finalized")

        banned_parts: list[str] = []
        if payload.final:
            emitted = session.redactor.push(payload.chunk) + session.redactor.finish()
            session.finished = True
            closed = True
        else:
            emitted = session.redactor.push(payload.chunk)
            closed = False
        session.chunk_count += 1

        fresh, originals = session.drain_new_mappings()
        banned_parts.extend(originals.values())
        uncertain = list(session.redactor.uncertain)

        app.state.audit.record_request(
            request_id=rid,
            session_id=session.session_id,
            endpoint="/api/v1/sessions/chunk",
            ruleset=ruleset,
            input_length=session.redactor.input_length,
            output_length=session.redactor._out_pos,
            matches=len(session.redactor.mappings),
            uncertain=len(uncertain),
        )
        if fresh:
            app.state.audit.record_fragments(
                request_id=rid,
                session_id=session.session_id,
                mappings=fresh,
                originals_by_key=originals,
            )
        app.state.audit.append_event(
            "session.chunk",
            {
                "chunk_index": session.chunk_count,
                "final": payload.final,
                "new_mappings": len(fresh),
                "uncertain": _uncertain_dto(uncertain),
            },
            request_id=rid,
            session_id=session.session_id,
        )
        safe_log.info(
            "session.chunk",
            banned=banned_parts,
            request_id=rid,
            session_id=session.session_id,
            rule_profile=ruleset.profile,
            rule_version=ruleset.version,
            chunk_index=session.chunk_count,
            final=payload.final,
            input_length=session.redactor.input_length,
            output_length=session.redactor._out_pos,
            matches=len(session.redactor.mappings),
            uncertain=len(uncertain),
        )
        if closed:
            app.state.sessions.close(session.session_id)
        return S.StreamChunkResponse(
            request_id=rid,
            session_id=session.session_id,
            chunk_index=session.chunk_count,
            emitted=emitted,
            final=payload.final,
            mappings=[_mapping_dto(m) for m in fresh],
            uncertain=_uncertain_dto(uncertain),
            input_length=session.redactor.input_length,
            output_length=session.redactor._out_pos,
            closed=closed,
        )

    @app.get("/api/v1/audit/requests")
    async def audit_requests(
        request: Request,
        x_audit_key: Optional[str] = Header(default=None),
        limit: int = 100,
    ):
        app.state.audit.list_fragments  # touch to ensure attr exists
        # Authorization is enforced inside the store; do a cheap gate here too.
        from hmac import compare_digest

        if not compare_digest(x_audit_key or "", settings.audit_key):
            raise AuditDenied("invalid audit key")
        items = app.state.audit.list_requests(limit=min(max(limit, 1), 500))
        app.state.audit.append_event(
            "audit.list_requests",
            {"count": len(items)},
            request_id=request.state.request_id,
        )
        return {"request_id": request.state.request_id, "requests": items}

    @app.get("/api/v1/audit/requests/{request_id}")
    async def audit_request_detail(
        request_id: str,
        request: Request,
        reveal: bool = False,
        x_audit_key: Optional[str] = Header(default=None),
    ):
        meta = app.state.audit.get_request(request_id)
        fragments = app.state.audit.list_fragments(
            request_id,
            reveal=reveal,
            audit_key=x_audit_key or "",
            expected_key=settings.audit_key,
        )
        app.state.audit.append_event(
            "audit.get_request",
            {"target": request_id, "reveal": reveal, "fragments": len(fragments)},
            request_id=request.state.request_id,
        )
        return {
            "request_id": request.state.request_id,
            "request": meta,
            "fragments": fragments,
        }

    @app.get("/api/v1/audit/chain")
    async def audit_chain(
        request: Request, x_audit_key: Optional[str] = Header(default=None)
    ):
        from hmac import compare_digest

        if not compare_digest(x_audit_key or "", settings.audit_key):
            raise AuditDenied("invalid audit key")
        report = app.state.audit.verify_chain()
        return {"request_id": request.state.request_id, "chain": report}

    return app


def _valid_request_id(value: str) -> bool:
    return bool(value) and len(value) <= 64 and all(
        c.isalnum() or c in "_-" for c in value
    )


app = create_app()
