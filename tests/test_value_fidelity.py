"""存储值类型保真：bool/int/float/str/bytes/NULL 经 MERGEVAL 编解码精确还原。

SQLite 原生会把 bool 存成 INTEGER 0/1、把字节串/文本按亲和性互改；
本系统的数据列用带类型标记的 BLOB 编解码，决策与读回必须看到原始 Python 类型。
"""
from __future__ import annotations

import math

from merge_engine import MergeRequest

from conftest import cfg, seed


def test_seed_and_commit_roundtrip_preserves_types(tmp_path):
    rows_in = [
        {"k1": "bool", "k2": 1, "v": True},
        {"k1": "bool", "k2": 2, "v": False},
        {"k1": "int", "k2": 3, "v": -2 ** 60},
        {"k1": "float", "k2": 4, "v": 1.25},
        {"k1": "nan", "k2": 5, "v": float("inf")},
        {"k1": "str", "k2": 6, "v": "中文"},
        {"k1": "bytes", "k2": 7, "v": b"\x00\x01\xff"},
        {"k1": "null", "k2": 8, "v": None},
        {"k1": "empty", "k2": 9, "v": ""},
    ]
    eng = seed(tmp_path, "types", ["k1", "k2", "v"], rows_in)

    out = {(r["k1"], r["k2"]): r["v"] for r in eng.get_target_rows("types")}
    checks = {
        ("bool", 1): (True, bool),
        ("bool", 2): (False, bool),
        ("int", 3): (-2 ** 60, int),
        ("float", 4): (1.25, float),
        ("nan", 5): (float("inf"), float),
        ("str", 6): ("中文", str),
        ("bytes", 7): (b"\x00\x01\xff", bytes),
        ("null", 8): (None, type(None)),
        ("empty", 9): ("", str),
    }
    for key, (value, typ) in checks.items():
        got = out[key]
        if isinstance(value, float) and math.isinf(value):
            assert math.isinf(got) and type(got) is float
        else:
            assert got == value and type(got) is typ, (key, got, type(got))


def test_update_preserves_bool_distinction(tmp_path):
    eng = seed(tmp_path, "tb", ["k1", "k2", "flag"], [
        {"k1": "a", "k2": 1, "flag": True},
    ])
    # 源把 True 改成 False：若 bool 退化成 0/1 比较仍会写，但读回类型必须是 bool
    r = eng.run(MergeRequest(
        source={"format": "records", "records": [{"k1": "a", "k2": 1, "flag": False}]},
        config=cfg("tb", ("k1", "k2")),
    ))
    assert r.status == "COMMITTED"
    rows = eng.get_target_rows("tb")
    assert rows[0]["flag"] is False
    assert type(rows[0]["flag"]) is bool


def test_insert_types_via_engine(tmp_path):
    eng = seed(tmp_path, "ti", ["k1", "k2", "v"], [])
    r = eng.run(MergeRequest(
        source={"format": "records", "records": [
            {"k1": "s", "k2": 1, "v": "x"},
            {"k1": "b", "k2": 2, "v": b"bin"},
            {"k1": "n", "k2": 3, "v": None},
        ]},
        config=cfg("ti", ("k1", "k2")),
    ))
    assert r.status == "COMMITTED"
    out = {(x["k1"], x["k2"]): x["v"] for x in eng.get_target_rows("ti")}
    assert out[("s", 1)] == "x" and type(out[("s", 1)]) is str
    assert out[("b", 2)] == b"bin" and type(out[("b", 2)]) is bytes
    assert out[("n", 3)] is None


def test_keys_compare_with_real_types_not_encoded_blob(tmp_path):
    """端到端：键比较在解码后的 Python 值上进行（不是比编码字节）。"""
    eng = seed(tmp_path, "tk", ["k1", "k2"], [{"k1": 7, "k2": 8}])
    r = eng.run(MergeRequest(
        source={"format": "records", "records": [{"k1": 7, "k2": 8}]},
        config=cfg("tk", ("k1", "k2")),
    ))
    assert r.counts["UPDATE_MATCHED"] == 1
    assert r.counts["INSERT_UNMATCHED"] == 0
