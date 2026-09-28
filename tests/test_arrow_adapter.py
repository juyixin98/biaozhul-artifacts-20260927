"""Arrow IPC adapter tests: build Arrow input, post it, decode the stream."""
from __future__ import annotations

import base64

import pyarrow as pa
import pyarrow.compute as pc

from .conftest import assert_error


def _stream(columns):
    """columns: list of (name, value_list, index_list_with_nulls)."""
    arrays, fields = [], []
    for name, values, indices in columns:
        dict_arr = pa.DictionaryArray.from_arrays(
            pa.array(indices, type=pa.int8()), pa.array(values,
                                                       type=pa.utf8()))
        arrays.append(dict_arr)
        fields.append(pa.field(name, dict_arr.type))
    table = pa.table({f.name: a for f, a in zip(fields, arrays)},
                     schema=pa.schema(fields))
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as w:
        w.write_table(table)
    return sink.getvalue().to_pybytes()


def test_arrow_encode_roundtrip(client):
    # Arrow tables are rectangular: the shorter batch b2 is padded with
    # NULL rows (bitmap independent of index codes).
    body = _stream([
        ("b1", ["a", "b", "a"], [0, 1, 2, 0, None]),
        ("b2", ["z", "b"], [0, 1, 0, None, None]),
    ])
    resp = client.post("/v1/encode/arrow?target_width=8",
                       content=body,
                       headers={"content-type": "application/vnd.apache.arrow"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    gd = [(e["type"], e["value"]) for e in
          data["global_dictionary"]["entries"]]
    assert gd == [("utf8", "a"), ("utf8", "b"), ("utf8", "z")]
    b1 = data["batches"][0]
    assert b1["local_to_global"] == [0, 1, 0]
    assert b1["global_indices"] == [0, 1, 0, 0, None]
    assert b1["valid"] == [True, True, True, True, False]
    b2 = data["batches"][1]
    assert b2["local_to_global"] == [2, 1]
    assert b2["global_indices"] == [2, 1, 2, None, None]
    assert b2["valid"] == [True, True, True, False, False]

    # The remap IPC stream is one vertical table; filter b1 and decode.
    stream = pa.ipc.open_stream(
        pa.BufferReader(base64.b64decode(data["arrow_remap_base64"])))
    table = stream.read_all()
    mask = pc.equal(
        table.column("batch_id"), pa.scalar("b1")).to_pylist()
    codes = [c for c, m in zip(
        table.column("global_code").to_pylist(), mask) if m]
    validity = [v for v, m in zip(
        table.column("valid").to_pylist(), mask) if m]
    global_values = [e["value"] for e in
                     data["global_dictionary"]["entries"]]
    decoded = [None if not v else global_values[c]
               for c, v in zip(codes, validity)]
    assert decoded == ["a", "b", "a", "a", None]


def test_arrow_non_dictionary_column_is_classified_error(client):
    plain = pa.table({"b": pa.array(["a", "b"])})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, plain.schema) as w:
        w.write_table(plain)
    resp = client.post(
        "/v1/encode/arrow",
        content=sink.getvalue().to_pybytes(),
        headers={"content-type": "application/vnd.apache.arrow"})
    assert_error(resp, 400, "REQUEST_MALFORMED")


def test_arrow_unsupported_value_type_is_classified(client):
    arr = pa.DictionaryArray.from_arrays(
        pa.array([0], type=pa.int8()), pa.array([1.5], type=pa.float64()))
    table = pa.table({"b": arr})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as w:
        w.write_table(table)
    resp = client.post(
        "/v1/encode/arrow",
        content=sink.getvalue().to_pybytes(),
        headers={"content-type": "application/vnd.apache.arrow"})
    assert resp.status_code == 422
    assert resp.json()["error"]["category"] == "UNSUPPORTED_VALUE_TYPE"
