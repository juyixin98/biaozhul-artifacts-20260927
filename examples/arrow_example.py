#!/usr/bin/env python3
"""Arrow IPC example: build two dictionary-encoded columns locally and POST.

Run while the server is up (./run.sh).
"""
import base64
import json
import urllib.request

import pyarrow as pa
import pyarrow.ipc as ipc

import os

BASE = os.environ.get("BASE", "http://127.0.0.1:8000")

b1 = pa.DictionaryArray.from_arrays(
    pa.array([0, 1, 2, 0, None], type=pa.int8()),
    pa.array(["a", "b", "a"], type=pa.utf8()))
b2 = pa.DictionaryArray.from_arrays(
    pa.array([0, 1, 0, None, None], type=pa.int8()),
    pa.array(["z", "b"], type=pa.utf8()))
table = pa.table({"b1": b1, "b2": b2})

sink = pa.BufferOutputStream()
with ipc.new_stream(sink, table.schema) as writer:
    writer.write_table(table)
payload = sink.getvalue().to_pybytes()

req = urllib.request.Request(
    f"{BASE}/v1/encode/arrow?target_width=8&width_policy=reject",
    data=payload, headers={"content-type": "application/vnd.apache.arrow"})
out = json.load(urllib.request.urlopen(req))
print("global dictionary:",
      [(e["type"], e["value"]) for e in
       out["global_dictionary"]["entries"]])
print("b1 global_indices:", out["batches"][0]["global_indices"])
print("b1 valid:         ", out["batches"][0]["valid"])
print("b2 global_indices:", out["batches"][1]["global_indices"])

# Independently decode the returned remap IPC stream.
stream = ipc.open_stream(
    pa.BufferReader(base64.b64decode(out["arrow_remap_base64"])))
rt = stream.read_all()
print("arrow remap rows:", rt.column("batch_id").to_pylist(),
      rt.column("global_code").to_pylist(), rt.column("valid").to_pylist())
