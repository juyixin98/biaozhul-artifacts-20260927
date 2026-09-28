"""End-to-end demonstration / manual verification script.

Runs entirely against the in-process service with synthetic fixtures (no
network, no real data). Exercises exactly the reviewer scenarios:

  1. import a primitive column with NULLs
  2. slice it at a NON-ZERO offset that crosses the bitmap byte boundary,
     asserting NULL flags and indices match an independent expectation and
     PyArrow
  3. empty string vs NULL distinction in a utf8 column
  4. illegal DECREASING offsets -> categorized failure, never "success"
  5. cross-type concat rejected without target_type; succeeds with explicit cast
  6. prints the real bytes copied for each concat

Every step is logged with a run id and input fingerprint. Run:

    python scripts/demo.py
"""
from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

# Allow running directly: python scripts/demo.py from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow as pa

from app.adapters.descriptor import descriptor_to_raw
from app.core.concat import concat
from app.errors import LayoutError
from app.logging_setup import bind_context, configure_logging, get_logger, new_run_id
from app.adapters.importer import import_raw
from app.validation.checks import validate
from tests.fixtures.oracle import fixed_descriptor, string_descriptor

LOG = get_logger("demo")


def section(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main() -> None:
    configure_logging("INFO", "dev")
    tmpdir = tempfile.mkdtemp(prefix="arrow_demo_")
    print(f"synthetic workspace: {tmpdir}")
    print(f"versions: pyarrow={pa.__version__}")

    # 1 + 2: primitive import and a non-zero-offset slice across a byte boundary.
    with bind_context(new_run_id(), None) as ctx:
        section(f"[run={ctx['run_id']}] 1/2 primitive import + non-zero-offset slice")
        values = [i * 10 for i in range(16)]
        values[7] = None   # NULL exactly on the first bitmap byte boundary
        values[9] = None
        desc = fixed_descriptor("int16", values)
        raw = descriptor_to_raw(desc)
        report = validate(raw)
        assert report.ok, report.failure_categories
        print(f"validation: ok, computed_null_count={report.computed_null_count}")
        view = import_raw(raw, validation_values=report.values).view
        sub = view.slice(7, 6)
        expected = [None, 80, None, 100, 110, 120]
        got = sub.to_pylist()
        print(f"slice(7,6) logical values: {got}")
        print(f"slice null flags       : {[sub.is_null(i) for i in range(sub.length)]}")
        print(f"buffers shared w/ parent: {sub.shares_memory_with(view)} (zero-copy)")
        print(f"PyArrow reference       : {pa.array(values, pa.int16()).slice(7, 6).to_pylist()}")
        assert got == expected
        assert sub.materialize().to_pylist() == pa.array(values, pa.int16()).slice(7, 6).to_pylist()
        print("RESULT: indices and NULLs at offset!=0 are correct and match PyArrow")

    # 3: empty string vs NULL.
    with bind_context(new_run_id(), None) as ctx:
        section(f"[run={ctx['run_id']}] 3 utf8 empty-string vs NULL")
        values = ["alpha", "", None, "", "zz", None]
        raw = descriptor_to_raw(string_descriptor(values))
        report = validate(raw)
        view = import_raw(raw, validation_values=report.values).view
        print(f"values     : {view.to_pylist()}")
        print(f"is_null    : {[view.is_null(i) for i in range(view.length)]}")
        print(f"null_count : {view.logical_null_count()} (empty strings are NOT null)")
        assert view.to_pylist() == values
        assert view.logical_null_count() == 2

    # 4: illegal decreasing offsets.
    with bind_context(new_run_id(), None) as ctx:
        section(f"[run={ctx['run_id']}] 4 illegal decreasing offsets")
        def mutate(offsets, data):
            offsets[2] = offsets[1] - 2
        raw = descriptor_to_raw(string_descriptor(["abc", "de", "f"], mutate=mutate))
        report = validate(raw)
        print(f"validation ok           : {report.ok}")
        print(f"failure categories      : {report.failure_categories}")
        bad = next(c for c in report.failures if c.category == "offsets_not_monotonic")
        print(f"evidence: {bad.evidence}")
        assert report.ok is False
        assert "offsets_not_monotonic" in report.failure_categories
        # The import path is gated on validation inside import_raw as well, so
        # the corrupt offsets never reach Arrow's constructor.
        try:
            import_raw(raw)
            raise SystemExit("ERROR: invalid offsets were accepted")
        except LayoutError as exc:
            print(f"import gate rejected before construction: category={exc.category.value}")

    # 5 + 6: cross-type concat rule and copy accounting.
    with bind_context(new_run_id(), None) as ctx:
        section(f"[run={ctx['run_id']}] 5/6 cross-type concat and copy accounting")
        a = import_raw(descriptor_to_raw(fixed_descriptor("int32", [1, 2]))).view
        b = import_raw(descriptor_to_raw(fixed_descriptor("int64", [3, 4]))).view
        try:
            concat([a, b])
            raise SystemExit("ERROR: cross-type concat accepted without cast")
        except LayoutError as exc:
            print(f"without target_type -> {exc.category.value}: {exc.detail}")
        result = concat([a, b], target_type="int64")
        print(f"with target_type=int64 -> {result.view.to_pylist()}")
        cr = result.report.to_dict()
        print(f"copied bytes: {cr['copied_bytes']}")
        for step in cr["steps"]:
            print(f"  step: {step}")
        assert result.view.to_pylist() == [1, 2, 3, 4]
        print("RESULT: explicit cast applied; real copy volume reported, not assumed zero-copy")

    section("ALL DEMO CHECKS PASSED")
    print(f"(metadata would be written under: {Path(tmpdir)})")


if __name__ == "__main__":
    main()
