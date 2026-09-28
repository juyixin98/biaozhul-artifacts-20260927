"""Differential tests vs PyArrow (values) and fastparquet (page D/R).

The reference answers come from two independent implementations -- never from
the kernel under test.
"""
from __future__ import annotations

import random

import pyarrow.parquet as pq
import pytest

from app.adapters import level_oracle
from app.adapters.pyarrow_adapter import build_arrow_table
from app.core.kernel import decode_table, encode_table
from app.core.schema import Schema
from app.core.verifier import verify


@pytest.mark.parametrize("seed", range(10))
def test_random_list_of_list_matches_pyarrow_values(seed, tmp_path):
    random.seed(seed)

    def gen(depth: int):
        r = random.random()
        if depth == 0:
            return random.choice([random.randint(-20, 20), None])
        if r < 0.2:
            return None
        if r < 0.35:
            return []
        return [gen(depth - 1) for _ in range(random.randint(0, 4))]

    sch = Schema({"name": "root", "type": "struct", "children": [
        {"name": "d", "type": "list",
         "item": {"type": "list", "item": {"type": "int32"}}}]})
    recs = [{"d": gen(2)} for _ in range(400)]
    pq.write_table(build_arrow_table(sch, recs), tmp_path / "f.parquet")
    arrow = pq.read_table(tmp_path / "f.parquet").to_pydict()["d"]
    kernel = [r["d"] for r in decode_table(encode_table(sch, recs))]
    assert kernel == arrow


def test_verifier_all_steps_pass_for_list_contract(tmp_path):
    sch = Schema({"name": "root", "type": "struct", "children": [
        {"name": "a", "type": "list",
         "item": {"name": "element", "type": "int32"}},
        {"name": "b", "type": "list",
         "item": {"name": "element", "type": "list",
                   "item": {"name": "element", "type": "int32"}}}]})
    recs = [
        {"a": [1, None, 3], "b": [[1, 2], [None]]},
        {"a": [], "b": []},
        {"a": None, "b": None},
        {"a": [4], "b": [[], [3, None]]},
    ]
    result = verify(sch, recs, workdir=tmp_path, page_slot_target=4,
                    expected=recs, parquet_page_bytes=64)
    assert result["status"] == "OK"
    phases = {s["step"] for s in result["steps"]}
    assert {"kernel_encode", "kernel_decode", "expected_tree",
            "kernel_paging", "pyarrow_write_read",
            "pyarrow_value_oracle", "fastparquet_level_oracle"} <= phases
    assert result["findings"] == []


def test_page_level_oracle_levels_match_and_pages_start_r0(tmp_path):
    sch = Schema({"name": "root", "type": "struct", "children": [
        {"name": "a", "type": "list",
         "item": {"name": "element", "type": "int32"}}]})
    recs = [{"a": list(range(i, i + 8))} for i in range(1500)]
    result = verify(sch, recs, workdir=tmp_path, page_slot_target=25,
                    parquet_page_bytes=128)
    assert result["status"] == "OK"
    # external file genuinely spans multiple pages
    assert result["oracle_page_count"]["a.list.element"] > 1
    pages = level_oracle.extract_pages(tmp_path / "candidate.parquet")
    for col in pages:
        for p in col.pages:
            if p.repetition_levels:
                assert p.repetition_levels[0] == 0, (
                    "page boundary truncates a record")


def test_expected_tree_mismatch_is_fatal(tmp_path):
    sch = Schema({"name": "root", "type": "struct", "children": [
        {"name": "a", "type": "list", "item": {"type": "int32"}}]})
    recs = [{"a": [1, 2]}]
    wrong_expected = [{"a": [1, 99]}]  # hand-written oracle disagrees
    result = verify(sch, recs, workdir=tmp_path, page_slot_target=10,
                    expected=wrong_expected)
    codes = [f["code"] for f in result["findings"]]
    assert "ROUNDTRIP_MISMATCH" in codes


def test_error_locations_reference_page_and_record(tmp_path):
    sch = Schema({"name": "root", "type": "struct", "children": [
        {"name": "a", "type": "list",
         "item": {"name": "element", "type": "int32"}}]})
    recs = [{"a": list(range(0, 600, 3))} for _ in range(200)]
    result = verify(sch, recs, workdir=tmp_path, page_slot_target=20,
                    parquet_page_bytes=64)
    assert result["status"] == "OK"
    # kernel page report carries record ranges and first R for every page
    for col, pages in result["kernel_pages"].items():
        for p in pages:
            assert "record_start" in p and "record_end" in p
            assert p["first_repetition_level"] == 0
