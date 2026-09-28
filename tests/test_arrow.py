"""PyArrow adapter tests + property-based round-trip fuzzing against the
independent oracle."""
from __future__ import annotations

import itertools
import random

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from app.adapters import arrowio
from app.api.app import create_app
from app.config import Settings
from app.core.kernel import unify
from app.core.verify import verify_roundtrip
from app.store.sqlite_store import JobStore
from tests.fixtures import make_batch
from tests.oracle import oracle_unify


def _dict_array(values, index_type=pa.uint8(), metadata_batch_id=None):
    """Build a DictionaryArray from a pylist that may contain None."""
    distinct = []
    seen = {}
    indices = []
    for v in values:
        if v is None:
            indices.append(None)
        else:
            if v not in seen:
                seen[v] = len(distinct)
                distinct.append(v)
            indices.append(seen[v])
    idx = pa.array(indices, type=index_type)
    d = pa.array(distinct)
    arr = pa.DictionaryArray.from_arrays(idx, d)
    rb = pa.record_batch([arr], names=["value"])
    return rb


def test_arrow_adapter_null_index_becomes_validity_bitmap():
    rb = _dict_array(["a", "b", None, "a", None])
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, rb.schema) as w:
        w.write_batch(rb)
    batches = arrowio.decode_ipc_batches(sink.getvalue().to_pybytes(),
                                         value_type="string")
    b = batches[0]
    assert b.dictionary == ["a", "b"]
    assert b.validity == [True, True, False, True, False]
    assert b.indices == [0, 1, 0, 0, 0]  # padding on null rows


def test_arrow_roundtrip_preserves_all_rows_and_widths():
    rb0 = _dict_array(["a", "b", None, "a"])
    rb1 = _dict_array(["b", "c", "c", None])
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, rb0.schema) as w:
        w.write_batch(rb0)
        w.write_batch(rb1)
    batches = arrowio.decode_ipc_batches(sink.getvalue().to_pybytes(),
                                         value_type="string")
    result = unify(batches, value_type="string")
    verify_roundtrip(result, batches)
    assert result.global_dictionary == ("a", "b", "c")

    out_buf = arrowio.result_to_ipc(result)
    table = pa.ipc.open_stream(pa.BufferReader(out_buf)).read_all()
    assert table.column_names == ["batch-0", "batch-1"]
    assert table.column("batch-0").to_pylist() == ["a", "b", None, "a"]
    assert table.column("batch-1").to_pylist() == ["b", "c", "c", None]
    # unsigned index width is the auto-selected uint8
    assert table.column("batch-0").type.index_type == pa.uint8()


def test_arrow_endpoint_e2e(tmp_path):
    settings = Settings(db_path=tmp_path / "a.db",
                        max_cardinality=2**32 - 1, log_level="ERROR")
    store = JobStore(settings.db_path)
    app = create_app(settings=settings, store=store)
    rb = _dict_array(["x", "y", "x", None])
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, rb.schema) as w:
        w.write_batch(rb)
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/unify/arrow?value_type=string",
            content=sink.getvalue().to_pybytes(),
            headers={"content-type": "application/vnd.apache.arrow.stream"},
        )
        assert r.status_code == 200, r.text
        table = pa.ipc.open_stream(pa.BufferReader(r.content)).read_all()
        assert table.column("batch-0").to_pylist() == ["x", "y", "x", None]
    store.close()


def test_arrow_null_entry_in_dictionary_values_rejected():
    # Dictionary values themselves contain a null (not the indices).
    indices = pa.array([0, 1], type=pa.uint8())
    d = pa.array(["a", None])
    arr = pa.DictionaryArray.from_arrays(indices, d)
    rb = pa.record_batch([arr], names=["value"])
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, rb.schema) as w:
        w.write_batch(rb)
    from app.core.errors import NullDictionaryEntryError
    with pytest.raises(NullDictionaryEntryError):
        arrowio.decode_ipc_batches(sink.getvalue().to_pybytes(),
                                  value_type="string")


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_property_random_batches_match_oracle(seed, step_logger):
    rng = random.Random(seed)
    alphabet = [f"v{i}" for i in range(rng.choice([1, 2, 50, 300]))]
    raw_batches = []
    kernel_batches = []
    for bi in range(rng.randint(1, 4)):
        local = rng.sample(alphabet, k=rng.randint(1, min(len(alphabet), 20)))
        n = rng.randint(0, 12)
        indices = [rng.randrange(len(local)) for _ in range(n)]
        validity = [rng.random() > 0.3 for _ in range(n)]
        raw = make_batch(f"b{bi}", local, indices, validity)
        raw_batches.append(raw)
        kernel_batches.append(
            __import__("app.core.kernel", fromlist=["BatchInput"]).BatchInput(
                f"b{bi}", local, indices, validity)
        )

    result = unify(kernel_batches, value_type="string")
    step_logger.step("fuzz", seed=seed,
                     cardinality=result.cardinality,
                     width=result.index_width_bits,
                     batch_ids=[b["batch_id"] for b in raw_batches])

    oracle = oracle_unify(raw_batches, value_type="string")
    assert tuple(result.global_dictionary) == oracle.global_dictionary
    assert result.index_width_bits == oracle.width

    decoded = {d.batch_id: d.rows for d in verify_roundtrip(result, kernel_batches)}
    for bid, rows in oracle.decoded_rows.items():
        assert decoded[bid] == rows
