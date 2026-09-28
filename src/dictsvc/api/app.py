"""FastAPI application: JSON + Arrow endpoints and metadata/verification."""
from __future__ import annotations

import base64

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..adapters.arrow_in import parse_arrow_stream
from ..adapters.arrow_out import build_remap_stream
from ..adapters.json_in import parse_request
from ..config import get_settings
from ..core.errors import DictSvcError, RequestMalformed
from ..core.model import BatchInput, GlobalEncoding
from ..core.policy import SORT_POLICY
from ..metadata.store import MetadataStore
from ..service import RunLogger, Service


def create_app(settings=None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="dictsvc",
        version="1.0.0",
        description="Unified multi-batch dictionary encoding service")
    app.state.settings = settings
    app.state.store = MetadataStore(settings.sqlite_path)
    app.state.run_logger = RunLogger(settings)
    app.state.service = Service(
        app.state.store, app.state.run_logger, settings.version_info())

    @app.exception_handler(DictSvcError)
    async def _classified(request: Request, exc: DictSvcError):
        return JSONResponse(status_code=exc.http_status,
                            content=exc.to_dict())

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        # Unknown states are never reported as success.
        return JSONResponse(status_code=500, content={
            "ok": False,
            "error": {"category": "INTERNAL_ERROR",
                      "message": f"{type(exc).__name__}: {exc}"}})

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "status": "healthy"}

    @app.get("/version")
    async def version():
        return settings.version_info()

    @app.post("/v1/encode")
    async def encode(request: Request):
        body = await _json_body(request)
        batches, options = parse_request(body)
        return app.state.service.run(batches, options)

    @app.post("/v1/encode/arrow")
    async def encode_arrow(request: Request,
                           width_policy: str = "reject",
                           target_width: int = 8):
        raw = await request.body()
        if not raw:
            raise RequestMalformed("empty Arrow IPC body")
        batches = parse_arrow_stream(raw)
        # Options travel as query params; re-validate via the JSON policy.
        options = {"run_id": None, "target_width": target_width,
                   "width_policy": width_policy,
                   "on_duplicate_values": "merge"}
        if target_width not in (8, 16, 32, 64):
            raise RequestMalformed(
                "target_width must be one of (8,16,32,64)")
        if width_policy not in ("reject", "expand"):
            raise RequestMalformed(
                "width_policy must be 'reject' or 'expand'")
        result = app.state.service.run(batches, options)
        # Attach the remapped rows as an Arrow IPC stream alongside JSON.
        enc = _reconstruct_encoding(result, batches)
        stream = build_remap_stream(enc)
        result["arrow_remap_base64"] = base64.b64encode(stream).decode()
        result["arrow_note"] = (
            "decode arrow_remap_base64 as one IPC stream: a vertical table "
            "with batch_id/global_code/valid columns for every remapped row")
        return result

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str):
        return {"ok": True, "run": app.state.store.get_run(run_id)}

    @app.post("/v1/verify")
    async def verify(request: Request):
        """Re-decode a stored run and compare against supplied candidate
        rows. Candidate shape:
        {"run_id": "...", "batches": [{"batch_id": "b1",
           "rows": ["a", null, 3]}]}
        """
        body = await _json_body(request)
        run_id = body.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise RequestMalformed("run_id must be a non-empty string")
        stored = app.state.store.get_run(run_id)
        if stored["status"] != "ok":
            raise RequestMalformed(
                f"run {run_id!r} is not in ok state "
                f"(status={stored['status']})")
        candidates = body.get("batches")
        if not isinstance(candidates, list):
            raise RequestMalformed("'batches' must be a list")

        entries = stored["global_entries"]
        global_values = {e["global_code"]: e for e in entries}
        mismatches: list[dict] = []
        checked = 0
        found: set[str] = set()
        for cand in candidates:
            bid, rows = cand.get("batch_id"), cand.get("rows")
            if not isinstance(bid, str) or not isinstance(rows, list):
                raise RequestMalformed(
                    "each candidate needs string batch_id and list rows")
            found.add(bid)
            sbatch = next((b for b in stored["batches"]
                           if b["batch_id"] == bid), None)
            if sbatch is None:
                mismatches.append({"batch_id": bid, "row": -1,
                                   "reason": "UNKNOWN_BATCH"})
                continue
            gidx = sbatch["global_indices"]
            bvalid = sbatch["valid"]
            if len(rows) != sbatch["row_count"]:
                mismatches.append({"batch_id": bid, "row": -1,
                                   "reason": "ROW_COUNT",
                                   "expected": sbatch["row_count"],
                                   "actual": len(rows)})
                continue
            checked += len(rows)
            for row, candidate_val in enumerate(rows):
                # Independently RE-DECODE from stored global codes + bitmap.
                if not bvalid[row]:
                    expected_val = None
                else:
                    entry = global_values[gidx[row]]
                    expected_val = entry["value"]
                if (candidate_val is None) is not (expected_val is None) \
                        or candidate_val != expected_val:
                    mismatches.append({
                        "batch_id": bid, "row": row,
                        "reason": "VALUE_MISMATCH",
                        "expected": expected_val,
                        "candidate": candidate_val})
        missing = {b["batch_id"] for b in stored["batches"]} - found
        for bid in sorted(missing):
            mismatches.append({"batch_id": bid, "row": -1,
                               "reason": "MISSING_BATCH"})
        ok = not mismatches
        return {"ok": True, "verification": {
            "all_match": ok, "checked_rows": checked,
            "mismatches": mismatches}}

    return app


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:
        raise RequestMalformed(f"request body is not valid JSON: {exc}") \
            from exc
    if not isinstance(body, dict):
        raise RequestMalformed("request body must be a JSON object")
    return body


def _reconstruct_encoding(result: dict,
                          batches: list[BatchInput]) -> GlobalEncoding:
    """Rebuild a lightweight GlobalEncoding for the Arrow output writer from
    a successful JSON result."""
    from ..core.model import BatchRemap, BatchStats
    gd = result["global_dictionary"]
    types = tuple(e["type"] for e in gd["entries"])
    values = tuple(e["value"] for e in gd["entries"])
    remaps = []
    for rb in result["batches"]:
        idx = tuple(-1 if x is None else x for x in rb["global_indices"])
        valid = tuple(rb["valid"])
        s = rb["stats"]
        remaps.append(BatchRemap(
            batch_id=rb["batch_id"],
            local_to_global=tuple(rb["local_to_global"]),
            global_indices=idx, valid=valid,
            stats=BatchStats(
                batch_id=rb["batch_id"], row_count=s["row_count"],
                null_rows=s["null_rows"],
                declared_entries=s["declared_entries"],
                distinct_values=s["distinct_values"],
                used_entries=s["used_entries"],
                unused_declared=s["unused_declared"],
                duplicate_declared=s["duplicate_declared"])))
    return GlobalEncoding(
        sort_policy=SORT_POLICY,
        width_policy=result["policy"]["width_policy"],
        target_width=result["policy"]["target_width"],
        global_index_width=result["policy"]["global_index_width"],
        global_types=types, global_values=values,
        cardinality=gd["cardinality"], batches=tuple(remaps))


app = create_app()
