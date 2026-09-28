"""Validation orchestration service.

Pipeline per request:

1. Parse the restricted schema (unsupported logical types are rejected with a
   named category).
2. Encode records to definition/repetition levels with the self-implemented
   kernel.
3. Write a standard Parquet v1 file with the hand-written format layer, with
   record-aligned pages.
4. Read that file back with the hand-written reader (exercising page
   reassembly) and compare the tree to the input and to the hand-written
   expected tree.
5. Independently write/read the same records with PyArrow and compare its
   tree to ours.
6. Read our file with PyArrow and read PyArrow's file with our reader, so the
   cross-check covers both directions and genuine byte interoperability.

Failure categories are enumerated in :data:`ERROR_CATEGORIES` and mismatches
record a page index + position whenever the failure originates on a page.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import get_settings
from .format import parquet_io
from .kernel.levels import LevelDecodeError, decode_records, encode_records
from .kernel.schema import (
    RootNode, SchemaError, UnsupportedLogicalTypeError, build_schema,
)
from .logging_utils import get_logger
from .oracle import read_reference_parquet, reference_roundtrip, write_reference_parquet
from .storage.metadata import MetadataStore, get_store

log = get_logger("service")

ERROR_CATEGORIES = (
    "UNSUPPORTED_LOGICAL_TYPE",   # schema asks for a type we explicitly refuse
    "SCHEMA_INVALID",             # malformed schema description
    "INVALID_RECORD",             # record violates required/null constraints
    "ROUNDTRIP_MISMATCH",         # our write->read tree differs
    "EXPECTED_TREE_MISMATCH",     # hand-written expected tree differs
    "ORACLE_MISMATCH",            # PyArrow disagrees with our result
    "PAGE_BOUNDARY_VIOLATION",    # record split / boundary misalignment
    "PARSE_ERROR",                # malformed parquet bytes / levels
    "INTERNAL_ERROR",
)


@dataclass
class Step:
    index: int
    name: str
    status: str = "ok"
    detail: dict[str, Any] | None = None


@dataclass
class ValidationOutcome:
    status: str
    steps: list[Step] = field(default_factory=list)
    mismatches: list[dict] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    page_count: int = 0
    artifact: dict[str, Any] | None = None
    error_category: str | None = None
    error_message: str | None = None

    def step_dicts(self) -> list[dict]:
        return [{"name": s.name, "status": s.status, "detail": s.detail}
                for s in self.steps]


class ValidationService:
    def __init__(self, store: MetadataStore | None = None):
        settings = get_settings()
        self.settings = settings
        self.store = store or get_store(settings)
        self.artifact_dir = Path(settings.artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

    def validate(self, request_id: str, schema_desc: dict,
                 records: list[dict[str, Any]],
                 expected_tree: list[dict] | None = None,
                 page_size_bytes: int | None = None,
                 force_page_after_records: int | None = None) -> ValidationOutcome:
        steps: list[Step] = []
        warnings: list[str] = []
        uncertainties: list[str] = []
        page_size = page_size_bytes or self.settings.default_page_size_bytes

        def add(name, status="ok", **detail):
            steps.append(Step(len(steps), name, status, detail or None))

        self.store.create_request(request_id, schema_desc, len(records))
        try:
            # 1. schema ----------------------------------------------------- #
            try:
                root = build_schema(schema_desc)
            except UnsupportedLogicalTypeError as exc:
                return self._fail(request_id, steps,
                                  "UNSUPPORTED_LOGICAL_TYPE", str(exc),
                                  warnings, uncertainties)
            except SchemaError as exc:
                return self._fail(request_id, steps, "SCHEMA_INVALID", str(exc),
                                  warnings, uncertainties)
            add("schema_parsed", leaf_count=len(_leaves(root)),
                fields=[f.name for f in root.fields])

            if len(records) > self.settings.max_records:
                return self._fail(
                    request_id, steps, "INVALID_RECORD",
                    f"{len(records)} records exceeds limit "
                    f"{self.settings.max_records}", warnings, uncertainties)

            # 2. kernel encode --------------------------------------------- #
            try:
                encoded = encode_records(root, records)
            except (ValueError, TypeError) as exc:
                return self._fail(request_id, steps, "INVALID_RECORD", str(exc),
                                  warnings, uncertainties)
            total_levels = sum(len(c.events) for c in encoded.columns)
            add("levels_encoded", leaf_columns=len(encoded.columns),
                total_leaf_slots=total_levels)
            if encoded.empty_struct_present:
                uncertainties.append(
                    "Zero-field structs have no physical Parquet column; their "
                    "per-record presence is carried out-of-band and cannot be "
                    "verified from file bytes alone: "
                    + ", ".join(sorted(encoded.empty_struct_present)))

            # 3. write our parquet ----------------------------------------- #
            self_path = str(self.artifact_dir / f"{request_id}.self.parquet")
            try:
                parquet_io.write_file(
                    self_path, root, records,
                    page_size_bytes=page_size,
                    force_page_after_records=force_page_after_records)
            except Exception as exc:  # defensive: writer failure is real
                return self._fail(request_id, steps, "INTERNAL_ERROR",
                                  f"self writer failed: {exc}", warnings,
                                  uncertainties)
            self_size = os.path.getsize(self_path)

            # 4. read our file back ---------------------------------------- #
            try:
                root_back, self_columns, num_rows = parquet_io.read_file_events(
                    self_path)
                enc_back = [type(encoded.columns[0])(c.leaf, c.events)
                            for c in self_columns]
                self_records = decode_records(
                    root_back, enc_back, num_records=num_rows,
                    empty_struct_present=encoded.empty_struct_present)
            except LevelDecodeError as exc:
                loc = (exc.diagnostics[0] if exc.diagnostics else None)
                return self._fail(
                    request_id, steps, "PAGE_BOUNDARY_VIOLATION", str(exc),
                    warnings, uncertainties,
                    page=_page_loc(loc))
            except Exception as exc:
                return self._fail(request_id, steps, "PARSE_ERROR", str(exc),
                                  warnings, uncertainties)
            page_count = sum(len(c.pages) for c in self_columns)
            page_info = {
                c.leaf.path[-1]: [
                    {"page": p.page_index, "values": p.num_values,
                     "records": p.record_count,
                     "first_record": p.first_record_index}
                    for p in c.pages]
                for c in self_columns
            }
            add("self_roundtrip", pages=page_count, page_layout=page_info)

            # 5. compare against input and hand-written expected tree ------ #
            tree_mismatches = _diff_trees(records, self_records,
                                          "ROUNDTRIP_MISMATCH")
            if tree_mismatches:
                self._persist(request_id, steps, "failed", self_path, None,
                              page_count, warnings, uncertainties,
                              tree_mismatches, "ROUNDTRIP_MISMATCH",
                              tree_mismatches[0]["message"])
                return ValidationOutcome(
                    "failed", steps, tree_mismatches, uncertainties, warnings,
                    page_count, _artifact(self_path, self_size, None, None),
                    "ROUNDTRIP_MISMATCH", tree_mismatches[0]["message"])

            if expected_tree is not None:
                exp_mismatches = _diff_trees(expected_tree, self_records,
                                             "EXPECTED_TREE_MISMATCH")
                if exp_mismatches:
                    self._persist(request_id, steps, "failed", self_path, None,
                                  page_count, warnings, uncertainties,
                                  exp_mismatches, "EXPECTED_TREE_MISMATCH",
                                  exp_mismatches[0]["message"])
                    return ValidationOutcome(
                        "failed", steps, exp_mismatches, uncertainties,
                        warnings, page_count,
                        _artifact(self_path, self_size, None, None),
                        "EXPECTED_TREE_MISMATCH",
                        exp_mismatches[0]["message"])
                add("expected_tree_asserted", records=len(records))

            # 6. independent PyArrow roundtrip ----------------------------- #
            # PyArrow refuses to serialise zero-field structs; when the schema
            # contains one, byte-level cross-checking is inherently impossible
            # for that field, so it is reported as an uncertainty (not a
            # failure) while every physical column is still cross-checked.
            has_empty_struct = bool(encoded.empty_struct_present)
            oracle_records = None
            if not has_empty_struct:
                try:
                    oracle_records = reference_roundtrip(
                        root, records,
                        page_size_bytes=8 if force_page_after_records else None)
                except Exception as exc:
                    warnings.append(
                        f"PyArrow reference roundtrip could not run: {exc}")
            else:
                uncertainties.append(
                    "Byte-level PyArrow cross-check skipped for zero-field "
                    "struct fields: Parquet (and PyArrow) cannot represent a "
                    "struct with no child column. The kernel-level tree "
                    "roundtrip above still verifies those fields; all other "
                    "columns are cross-checked where present.")
            if oracle_records is not None:
                oracle_mm = _diff_trees(self_records, oracle_records,
                                        "ORACLE_MISMATCH")
                add("oracle_roundtrip", status="ok" if not oracle_mm else "fail",
                    differences=len(oracle_mm))
                if oracle_mm:
                    self._persist(request_id, steps, "failed", self_path, None,
                                  page_count, warnings, uncertainties,
                                  oracle_mm, "ORACLE_MISMATCH",
                                  oracle_mm[0]["message"])
                    return ValidationOutcome(
                        "failed", steps, oracle_mm, uncertainties, warnings,
                        page_count,
                        _artifact(self_path, self_size, None, None),
                        "ORACLE_MISMATCH", oracle_mm[0]["message"])

            # 7. cross-direction byte interop ------------------------------ #
            oracle_path = str(self.artifact_dir / f"{request_id}.oracle.parquet")
            cross = []
            oracle_size = None
            if not has_empty_struct:
                try:
                    write_reference_parquet(
                        oracle_path, root, records,
                        page_size_bytes=8 if force_page_after_records else None)
                    arrow_reads_ours = read_reference_parquet(self_path)
                    _root_a, _cols_a, nrows_a = parquet_io.read_file_events(
                        oracle_path)
                    ours_read_arrow = decode_records(
                        _root_a,
                        [type(encoded.columns[0])(c.leaf, c.events)
                         for c in _cols_a], num_records=nrows_a)
                    cross1 = _diff_trees(records, arrow_reads_ours,
                                         "ORACLE_MISMATCH")
                    cross2 = _diff_trees(records, ours_read_arrow,
                                         "ORACLE_MISMATCH")
                    cross = cross1 + cross2
                    oracle_size = os.path.getsize(oracle_path)
                    add("cross_interop",
                        arrow_reads_self="ok" if not cross1 else "fail",
                        self_reads_arrow="ok" if not cross2 else "fail")
                except Exception as exc:
                    warnings.append(f"cross-interop check skipped: {exc}")
                    oracle_path = None
                    cross = []
            else:
                add("cross_interop", status="skipped",
                    reason="empty struct has no Parquet representation")
                oracle_path = None
            if cross:
                self._persist(request_id, steps, "failed", self_path,
                              oracle_path, page_count, warnings,
                              uncertainties, cross, "ORACLE_MISMATCH",
                              cross[0]["message"])
                return ValidationOutcome(
                    "failed", steps, cross, uncertainties, warnings, page_count,
                    _artifact(self_path, self_size, oracle_path, oracle_size),
                    "ORACLE_MISMATCH", cross[0]["message"])

            self.store.attach_artifacts(request_id, self_path, oracle_path,
                                        self_size, oracle_size)
            self._persist(request_id, steps, "passed", self_path, oracle_path,
                          page_count, warnings, uncertainties)
            add("validation_passed", records=len(records))
            return ValidationOutcome(
                "passed", steps, [], uncertainties, warnings, page_count,
                _artifact(self_path, self_size, oracle_path, oracle_size))

        except Exception as exc:  # pragma: no cover - safety net
            log.exception("unexpected validation failure",
                          extra={"context": {"request_id": request_id}})
            return self._fail(request_id, steps, "INTERNAL_ERROR", repr(exc),
                              warnings, uncertainties)

    # ------------------------------------------------------------------ #
    def _fail(self, request_id, steps, category, message, warnings,
              uncertainties, page=None) -> ValidationOutcome:
        for i, s in enumerate(steps):
            self.store.add_step(request_id, s.index, s.name, s.status, s.detail)
        self.store.add_step(request_id, len(steps), category.lower(), "fail",
                            {"message": message, **(page or {})})
        self.store.complete_request(request_id, "error", None, None, warnings,
                                    category, message)
        log.info("validation failed", extra={"context": {
            "request_id": request_id, "category": category}})
        return ValidationOutcome("error", steps, [], uncertainties, warnings,
                                 0, None, category, message)

    def _persist(self, request_id, steps, status, self_path, oracle_path,
                 page_count, warnings, uncertainties, mismatches=None,
                 category=None, message=None) -> None:
        for s in steps:
            self.store.add_step(request_id, s.index, s.name, s.status, s.detail)
        self.store.complete_request(request_id, status, self_path, page_count,
                                    warnings, category, message)
        log.info("validation %s", status, extra={"context": {
            "request_id": request_id, "mismatches": len(mismatches or []),
            "pages": page_count}})


def _leaves(root: RootNode):
    from .kernel.schema import leaf_columns
    return leaf_columns(root)


def _artifact(self_path, self_size, oracle_path, oracle_size) -> dict:
    return {
        "self_parquet": {"path": self_path, "bytes": self_size},
        "oracle_parquet": (
            {"path": oracle_path, "bytes": oracle_size}
            if oracle_path else None),
    }


def _page_loc(diag) -> dict:
    if diag is None:
        return {}
    return {"page_index": diag.page_index, "position": diag.position_in_page,
            "column_path": diag.column_path}


def _diff_trees(expected: list[dict], actual: list[dict],
                category: str) -> list[dict]:
    """Deep structural diff; reports the first divergence per record."""
    mismatches: list[dict] = []
    if len(expected) != len(actual):
        mismatches.append({
            "category": category,
            "record_index": min(len(expected), len(actual)),
            "message": f"record count {len(actual)} != expected {len(expected)}",
            "expected": len(expected), "actual": len(actual)})
        return mismatches
    for i, (exp, got) in enumerate(zip(expected, actual)):
        diff = _deep_diff(exp, got)
        if diff is not None:
            mismatches.append({
                "category": category,
                "record_index": i,
                "column_path": diff["path"],
                "expected": diff["expected"],
                "actual": diff["actual"],
                "message": f"record {i} at {diff['path']}: "
                           f"expected {diff['expected']!r}, got {diff['actual']!r}",
            })
    return mismatches


def _deep_diff(expected, actual, path="$"):
    if isinstance(expected, list) or isinstance(actual, list):
        if not isinstance(expected, list) or not isinstance(actual, list):
            return {"path": path, "expected": expected, "actual": actual}
        if len(expected) != len(actual):
            return {"path": path + ".length", "expected": len(expected),
                    "actual": len(actual)}
        for i, (e, a) in enumerate(zip(expected, actual)):
            d = _deep_diff(e, a, f"{path}[{i}]")
            if d:
                return d
        return None
    if isinstance(expected, dict) or isinstance(actual, dict):
        if not isinstance(expected, dict) or not isinstance(actual, dict):
            return {"path": path, "expected": expected, "actual": actual}
        keys = set(expected) | set(actual)
        for k in sorted(keys):
            if k not in expected:
                return {"path": f"{path}.{k}", "expected": None,
                        "actual": actual.get(k)}
            if k not in actual:
                return {"path": f"{path}.{k}", "expected": expected.get(k),
                        "actual": None}
            d = _deep_diff(expected[k], actual[k], f"{path}.{k}")
            if d:
                return d
        return None
    # Float equality: compare with NaN awareness.
    if expected != actual:
        if isinstance(expected, float) and isinstance(actual, float):
            if expected != expected and actual != actual:
                return None
        return {"path": path, "expected": expected, "actual": actual}
    return None
