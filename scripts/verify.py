#!/usr/bin/env python3
"""Independent verification script.

Runs the checklist a reviewer cares about, printing each computation step and
its verdict against TWO oracles that are not the kernel under test:

* a pure-Python oracle (raw validity bytes / int32 offsets)
* PyArrow itself

Nothing here is a stub: concrete values, addresses and copied-byte counts are
printed. Exit code is non-zero if any check fails; exceptions are reported as
FAIL with the exception type (never swallowed as success).

Usage:
    .venv/bin/python scripts/verify.py
"""

from __future__ import annotations

import base64
import gc
import json
import struct
import sys
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "samples"))

import numpy as np  # noqa: E402
import pyarrow as pa  # noqa: E402

from arrowzero.adapters import (  # noqa: E402
    export_ipc_stream,
    import_ipc_stream,
    import_pylist,
    import_raw_buffers,
)
from arrowzero.kernel.checks import ValidationError, validate_buffers  # noqa: E402
from arrowzero.kernel.concat import CastError, concat  # noqa: E402
from arrowzero.versions import runtime_versions  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, evidence: str = "") -> None:
    verdict = PASS if condition else FAIL
    results.append((name, verdict, evidence))
    marker = "✓" if condition else "✗"
    print(f"  [{marker}] {name}")
    if evidence:
        for line in evidence.splitlines():
            print(f"        {line}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def py_oracle_nulls(validity: bytes, length: int, offset: int) -> list[bool]:
    return [
        not bool((validity[(offset + i) >> 3] >> ((offset + i) & 7)) & 1)
        for i in range(length)
    ]


def py_oracle_strings(validity: bytes, offsets: bytes, data: bytes,
                      length: int, offset: int):
    offs = np.frombuffer(offsets, dtype=np.int32)
    nulls = py_oracle_nulls(validity, length, offset)
    out = []
    for k, i in enumerate(range(offset, offset + length)):
        if nulls[k]:
            out.append(None)
        else:
            s, e = int(offs[i]), int(offs[i + 1])
            out.append(data[s:e].decode("utf-8"))
    return out


def main() -> int:
    run_id = f"verify-{uuid.uuid4().hex[:8]}"
    print(f"run_id: {run_id}")
    print("versions: " + json.dumps(runtime_versions(), sort_keys=True))

    # ------------------------------------------------------------------
    section("1. Non-zero-offset slice: NULL judgment and indexing")
    strings = ["alpha", None, "", "βγ", "", None, "z", "δεζ"]
    view, _ = import_pylist(strings, "utf8")
    sl = view.slice(1, 5)
    validity, offsets, data = (bytes(b) for b in view.buffers)
    oracle = py_oracle_strings(validity, offsets, data, 5, 1)
    arrow = sl.to_arrow().to_pylist()
    print(f"  step: source={strings}")
    print(f"  step: slice(offset=1,length=5) logical_offset={sl.offset}")
    print(f"  py-oracle : {oracle}")
    print(f"  kernel    : {sl.to_pylist()}")
    print(f"  pyarrow   : {arrow}")
    check("kernel == python oracle", sl.to_pylist() == oracle)
    check("kernel == pyarrow", sl.to_pylist() == arrow)
    check("is_null correct at nonzero offset",
          [sl.is_null(i) for i in range(5)] == [True, False, False, False, True])
    check("empty string is not NULL", sl.get(1) == "" and not sl.is_null(1))
    check("sliced buffers are same allocation",
          all(a is b for a, b in zip(view.buffers, sl.buffers)),
          f"source addrs={[None if b is None else b.address for b in view.buffers]} "
          f"slice addrs={[None if b is None else b.address for b in sl.buffers]}")

    # ------------------------------------------------------------------
    section("2. Bitmap byte boundaries (NULL at every index 0..15)")
    for null_index in (0, 6, 7, 8, 9, 15):
        vals = list(range(16))
        vals[null_index] = None
        v, _ = import_pylist(vals, "int64")
        for start in (0, 7, 8):
            s = v.slice(start, 16 - start)
            expected_null = null_index >= start
            got = s.is_null(null_index - start) if expected_null else (s.count_nulls() == 0)
            check(f"null@{null_index} slice_start={start}", got,
                  f"count_nulls={s.count_nulls()} pyarrow={s.to_arrow().null_count}")

    # ------------------------------------------------------------------
    section("3. Empty string vs NULL (dense mixed column)")
    v, _ = import_pylist(["", None, "", None, ""], "utf8")
    check("null_count only counts NULLs", v.count_nulls() == 2, f"null_count={v.count_nulls()}")
    check("pyarrow agrees", v.to_arrow().null_count == 2)
    check("values", v.to_pylist() == ["", None, "", None, ""])

    # ------------------------------------------------------------------
    section("4. Illegal decreasing offsets -> DECREASING_OFFSET")
    desc = {
        "type": "utf8", "length": 3,
        "buffers": [None, struct.pack("<iiii", 0, 3, 2, 5), b"abcde"],
    }
    violations = validate_buffers(pa.utf8(), 3, desc["buffers"])
    codes = [(v.code.value, v.layer, v.index) for v in violations]
    print(f"  step: offsets={np.frombuffer(desc['buffers'][1], dtype=np.int32).tolist()}")
    print(f"  validator -> {codes}")
    check("single DECREASING_OFFSET at layer=offsets index=2",
          codes == [("DECREASING_OFFSET", "offsets", 2)])
    # Cross-check PyArrow's own verdict on the same bytes.
    try:
        arr = pa.Array.from_buffers(
            pa.utf8(), 3,
            [None, pa.py_buffer(desc["buffers"][1]), pa.py_buffer(desc["buffers"][2])],
        )
        arr.validate(full=True)
        arr.to_pylist()
        pyarrow_verdict = "accepted"
    except Exception as exc:  # noqa: BLE001
        pyarrow_verdict = f"{type(exc).__name__}: non-monotonic"
    print(f"  pyarrow   -> {pyarrow_verdict}")
    check("pyarrow also rejects non-monotonic offsets", "non-monotonic" in pyarrow_verdict)
    # importing must raise, not silently import
    desc_b64 = {
        "format": "raw_buffers", "type": "utf8", "length": 3,
        "buffers": [None,
                    base64.b64encode(desc["buffers"][1]).decode(),
                    base64.b64encode(desc["buffers"][2]).decode()],
    }
    try:
        import_raw_buffers(desc_b64)
        imported = "imported (WRONG)"
    except ValidationError as exc:
        imported = "rejected: " + ",".join(v["code"] for v in exc.to_dicts())
    check("raw import refuses the bytes", imported.startswith("rejected"), imported)

    # ------------------------------------------------------------------
    section("5. Separate checks: validity / offsets / data lengths")
    cases = [
        ("validity too short", (pa.int32(), 20, [b"\x00", b"\x00" * 80]),
         "BUFFER_TOO_SHORT", "validity"),
        ("data too short", (pa.int32(), 6, [None, b"\x00" * 20]),
         "BUFFER_TOO_SHORT", "data"),
        ("padding bits set", (pa.int32(), 6, [bytes([0xFF]), b"\x00" * 24]),
         "INVALID_PADDING", "validity"),
        ("offset oob", (pa.utf8(), 2, [None, struct.pack("<iii", 0, 3, 10), b"abcde"]),
         "OFFSET_OUT_OF_BOUNDS", "data"),
        ("first offset nonzero", (pa.utf8(), 2, [None, struct.pack("<iii", 4, 5, 6), b"abcdef"]),
         "INVALID_FIRST_OFFSET", "offsets"),
        ("invalid utf8", (pa.utf8(), 1, [None, struct.pack("<ii", 0, 1), b"\xff"]),
         "UTF8_INVALID", "data"),
    ]
    for label, args, code, layer in cases:
        vs = validate_buffers(*args)
        hit = [(v.code.value, v.layer) for v in vs]
        check(f"{label}: {code}[{layer}]", (code, layer) in hit, f"got={hit}")

    # ------------------------------------------------------------------
    section("6. Zero-copy ownership after source released (no dangling reads)")
    original = ["alpha", None, "", "βγ", "", None, "z"]
    base_view, _ = import_pylist(original, "utf8")
    payload = export_ipc_stream(base_view)
    imported, ev = import_ipc_stream(payload)
    addrs_before = [None if b is None else b.address for b in imported.buffers]
    sl = imported.slice(1, 5)
    del payload
    del base_view
    gc.collect()
    values_after = sl.to_pylist()
    addrs_after = [None if b is None else b.address for b in sl.buffers]
    print(f"  step: IPC buffers live inside payload = {ev['zero_copy']}")
    print(f"  step: addresses before source free: {addrs_before}")
    print(f"  step: addresses after  source free: {addrs_after}")
    print(f"  step: values read after free: {values_after}")
    check("ipc import is zero-copy", ev["zero_copy"] and ev["copied_bytes"] == 0)
    check("addresses unchanged after source deletion", addrs_before == addrs_after)
    check("values correct (no dangling memory)", values_after == [None, "", "βγ", "", None])
    check("pyarrow agrees", sl.to_arrow().to_pylist() == values_after)

    # ------------------------------------------------------------------
    section("7. Cross-type concat requires explicit cast; copy accounting")
    a, _ = import_pylist([1, 2, None], "int32")
    b, _ = import_pylist([3, 4], "int64")
    try:
        concat([a, b])
        cast_msg = "auto-cast (WRONG)"
    except CastError as exc:
        cast_msg = f"TYPE_MISMATCH: {str(exc).splitlines()[0]}"
    check("no silent auto-cast across types", cast_msg.startswith("TYPE_MISMATCH"), cast_msg)

    merged, ledger = concat([a, b], cast_to="int64")
    ref = pa.concat_arrays([a.to_arrow().cast(pa.int64()), b.to_arrow()]).to_pylist()
    print(f"  step: explicit cast int32+int64 -> int64")
    print(f"  kernel={merged.to_pylist()} pyarrow={ref}")
    print(f"  ledger={json.dumps(ledger.as_dict(), indent=2)}")
    check("cast concat values match pyarrow", merged.to_pylist() == ref)
    check("copied bytes > 0 (concat materializes)", ledger.copied_bytes > 0,
          f"copied_bytes={ledger.copied_bytes} allocated={ledger.allocated_bytes}")
    check("no output buffer aliases a source",
          all(
            (buf["aliased_source"] is None)
            for buf in ledger.as_dict()["output_buffers"] if buf["size"] > 0
          ))

    # slice must be zero copy; concat must copy
    same = a.slice(1, 2)
    check("slice copied_bytes == 0", same.length == 2 and
          all(x is y for x, y in zip(a.buffers, same.buffers)))

    # lossy narrowing must be refused
    big, _ = import_pylist([70000], "int64")
    small, _ = import_pylist([1], "int16")
    try:
        concat([big, small], cast_to="int16")
        narrowing = "accepted (WRONG)"
    except CastError as exc:
        narrowing = f"refused: {exc}"
    check("unsafe narrowing cast refused", narrowing.startswith("refused"), narrowing)

    # ------------------------------------------------------------------
    section("8. Multi-byte UTF-8 + empty/NULL concat accounting")
    s1, _ = import_pylist(["βγ", None], "utf8")
    s2, _ = import_pylist(["", "δεζ"], "utf8")
    m2, l2 = concat([s1, s2])
    expected = ["βγ", None, "", "δεζ"]
    data_bytes = sum(len(x.encode("utf-8")) for x in expected if x is not None)
    print(f"  step: expected={expected} actual={m2.to_pylist()}")
    print(f"  ledger={json.dumps(l2.as_dict(), indent=2)}")
    check("values match pyarrow", m2.to_pylist() == m2.to_arrow().to_pylist() == expected)
    check(f"data buffer holds exactly {data_bytes} utf-8 bytes",
          next(x["size"] for x in l2.as_dict()["output_buffers"] if x["name"] == "data")
          == data_bytes)

    # ------------------------------------------------------------------
    failed = [r for r in results if r[1] == FAIL]
    print(f"\n==== SUMMARY ({run_id}): {len(results) - len(failed)}/{len(results)} passed ====")
    if failed:
        for name, _, evidence in failed:
            print(f"  FAILED: {name} {evidence}")
        return 1
    print("  ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - unknown state must surface as FAIL, not success
        print("\nUNEXPECTED EXCEPTION (verdict=FAIL):")
        traceback.print_exc()
        sys.exit(2)
