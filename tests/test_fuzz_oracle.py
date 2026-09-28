"""Independent fuzz comparison against PyArrow's physical RL/DL bytes.

For random nested data we let PyArrow write Parquet files, decode the raw
definition/repetition levels with our own Thrift + hybrid decoder, and compare
those physical levels against the levels produced by the kernel under test.
Because PyArrow computes its levels independently, agreement here is an
external guarantee, not a self-fulfilling one.
"""
import os
import random
import struct
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.format.encodings import bit_width_for, decode_levels
from app.format.parquet_thrift import FileMetaData, PageHeader
from app.format.thrift import decode_struct
from app.kernel.levels import encode_records
from app.kernel.schema import build_schema, leaf_columns

SCHEMA = {
    "name": "fuzz",
    "fields": [
        {"name": "ids", "type": "list", "repetition": "optional",
         "element": {"type": "list", "element": {"type": "int32"}}},
    ],
}


def _random_records(seed: int, n: int) -> list[dict]:
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        choice = rnd.randrange(7)
        if choice == 0:
            ids = None
        elif choice == 1:
            ids = []
        elif choice == 2:
            ids = [None] * rnd.randrange(1, 4)
        elif choice == 3:
            ids = [[] for _ in range(rnd.randrange(1, 4))]
        else:
            ids = []
            for _ in range(rnd.randrange(1, 4)):
                inner = rnd.choice([None, []])
                if inner is None:
                    ids.append(None)
                elif inner == []:
                    ids.append([])
                else:
                    ids.append([rnd.randrange(-50, 50)
                                for _ in range(rnd.randrange(0, 4))])
            if all(x is not None and x != [] for x in ids):
                ids[0] = [rnd.randrange(-50, 50)]
        out.append({"ids": ids})
    return out


def _arrow_levels(path: str):
    """Return per-leaf (rep_levels, def_levels, values) decoded from bytes."""
    data = open(path, "rb").read()
    flen = len(data)
    fl = struct.unpack_from("<I", data, flen - 8)[0]
    md, _ = decode_struct(FileMetaData, data, flen - 8 - fl, flen - 8)
    results = []
    for rg in md["row_groups"]:
        for chunk in rg["columns"]:
            cm = chunk["meta_data"]
            offset = cm["data_page_offset"]
            rep_all, def_all = [], []
            while True:
                ph, he = decode_struct(PageHeader, data, offset, flen - 8)
                if ph.get("type") != 0:
                    break
                pay = data[he:he + ph["compressed_page_size"]]
                nv = ph["data_page_header"]["num_values"]
                pos = 0
                # repetition (max RL 2 for this schema)
                rl_len = struct.unpack_from("<I", pay, pos)[0]
                rep = decode_levels(pay, bit_width_for(2), nv, pos + 4).levels
                pos += 4 + rl_len
                dl_len = struct.unpack_from("<I", pay, pos)[0]
                dfl = decode_levels(pay, bit_width_for(5), nv, pos + 4).levels
                pos += 4 + dl_len
                rep_all.extend(rep)
                def_all.extend(dfl)
                offset = he + ph["compressed_page_size"]
                if cm["total_compressed_size"] <= offset - cm["data_page_offset"]:
                    break
            results.append((rep_all, def_all))
    return results


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 42, 99])
def test_kernel_levels_match_arrow_bytes(seed, tmp_path):
    records = _random_records(seed, 60)
    root = build_schema(SCHEMA)
    encoded = encode_records(root, records)
    leaf = leaf_columns(root)[0]
    self_rep = [e.repetition_level for e in encoded.columns[0].events]
    self_def = [e.definition_level for e in encoded.columns[0].events]

    # Build Arrow records from our random trees and force tiny pages so
    # bit-packed runs and page boundaries are both exercised.
    arrow_records = [r["ids"] for r in records]
    array = pa.array(arrow_records, type=pa.list_(pa.list_(pa.int32())))
    table = pa.table({"ids": array})
    path = str(tmp_path / f"fuzz-{seed}.parquet")
    pq.write_table(table, path, data_page_version="1.0",
                   use_dictionary=False, write_statistics=False,
                   compression="none", data_page_size=16)
    arrow_leaves = _arrow_levels(path)
    arrow_rep, arrow_def = arrow_leaves[0]

    assert self_rep == arrow_rep, (seed, self_rep[:40], arrow_rep[:40])
    assert self_def == arrow_def, (seed, self_def[:40], arrow_def[:40])
    assert leaf.max_repetition_level == 2
    assert leaf.max_definition_level == 5
