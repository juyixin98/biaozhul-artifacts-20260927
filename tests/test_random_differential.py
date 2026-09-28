"""随机差分测试：随机数据 + 随机谓词，内核选中集必须是真实匹配集的超集。

不使用 hypothesis（避免额外依赖），固定随机种子保证可复现。
参考真值仍由独立的 PyArrow 全扫描 validator 给出。
"""
from __future__ import annotations

import os
import random

import pyarrow as pa
import pytest

from pruning.adapter import ReadOptions, discover_table, write_parquet_file
from pruning.config import Config
from pruning.schemas import ValidateIn
from pruning.service import PruningService
from pruning.validation import FailureCategory

TYPES = {
    "event_ts": pa.timestamp("us", tz="UTC"),
    "amount": pa.int64(),
    "region": pa.string(),
}
REGIONS = ["alpha", "beta", "gamma", "delta", "longregion-zz-0001", None]


def _random_dataset(root, rng, n_parts, files_per_part, rows_per_file):
    table = "rand"
    months = ["2024-01", "2024-02", "2024-03", "2024-04"]
    import datetime as dt
    from pruning import transforms as T
    for pi in range(n_parts):
        month = months[pi % len(months)]
        y, m = T.parse_month_key(month)
        mstart = dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)
        ny, nm = T.add_months(y, m, 1)
        mend = dt.datetime(ny, nm, 1, tzinfo=dt.timezone.utc)
        span = int((mend - mstart).total_seconds())
        for fi in range(files_per_part):
            tss, amts, regs = [], [], []
            for _ in range(rows_per_file):
                # 时间戳必须落在本分区月内，保证桶值与原值一致
                sec = int(mstart.timestamp()) + rng.randrange(0, span)
                tss.append(None if rng.random() < 0.15 else sec)
                amts.append(None if rng.random() < 0.1 else rng.randrange(0, 1000))
                regs.append(rng.choice(REGIONS))
            path = os.path.join(root, table, f"event_ts={month}", f"p{pi}-{fi}.parquet")
            write_parquet_file(path, {"event_ts": tss, "amount": amts, "region": regs},
                               TYPES)


def _random_predicate(rng):
    col = rng.choice(["event_ts", "amount", "region"])
    kind = rng.choice(["range", "eq", "in", "is_null", "not_null"])
    if kind == "is_null":
        return {"column": col, "kind": "is_null"}
    if kind == "not_null":
        return {"column": col, "kind": "not_null"}
    if col == "event_ts":
        a, b = rng.randrange(1_704_067_200, 1_714_500_000), rng.randrange(1_704_067_200, 1_714_500_000)
        lo, hi = min(a, b), max(a, b)
        if kind == "range":
            return {"column": col, "kind": "range", "lower": lo, "upper": hi,
                    "lower_inclusive": rng.choice([True, False]),
                    "upper_inclusive": rng.choice([True, False])}
        if kind == "eq":
            return {"column": col, "kind": "eq", "value": rng.choice([lo, hi])}
        return {"column": col, "kind": "in", "values": [lo, hi]}
    if col == "amount":
        a, b = rng.randrange(0, 1000), rng.randrange(0, 1000)
        lo, hi = min(a, b), max(a, b)
        if kind == "range":
            return {"column": col, "kind": "range", "lower": lo, "upper": hi,
                    "upper_inclusive": rng.choice([True, False])}
        if kind == "eq":
            return {"column": col, "kind": "eq", "value": lo}
        return {"column": col, "kind": "in", "values": [lo, hi]}
    vals = ["alpha", "beta", "gamma", "delta", "longregion-zz-0001"]
    if kind == "eq":
        return {"column": col, "kind": "eq", "value": rng.choice(vals)}
    if kind == "in":
        return {"column": col, "kind": "in",
                "values": rng.sample(vals, k=rng.randrange(1, 3))}
    return {"column": col, "kind": "range",
            "lower": rng.choice(vals), "upper": rng.choice(vals),
            "upper_inclusive": rng.choice([True, False])}


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_randomized_zero_miss(tmp_path, seed):
    rng = random.Random(seed)
    root = str(tmp_path / "data")
    _random_dataset(root, rng, n_parts=4, files_per_part=3, rows_per_file=20)

    cfg = Config(data_root=root, db_path=str(tmp_path / f"c{seed}.sqlite"))
    svc = PruningService(cfg)
    # 一半注册把字符串统计截断，混合两种读取情形
    trunc = ["region"] if seed % 2 == 0 else []
    md = discover_table(root, "rand", "event_ts",
                        ReadOptions(truncated_string_columns=tuple(trunc),
                                    truncate_prefix_len=4))
    svc.catalog.register_table(md, root)

    for i in range(60):
        preds = [_random_predicate(rng) for _ in range(rng.randrange(1, 3))]
        r = svc.validate(ValidateIn(table="rand", request_id=f"f{seed}-{i}",
                                    predicates=preds))
        # 唯一的硬失败必须为零漏行
        hard = [f for f in r["failures"]
                if f["category"] != FailureCategory.SELECTED_BUT_EMPTY_MATCH]
        assert hard == [], f"seed={seed} i={i} preds={preds} hard={hard}"
        assert r["zero_missed_matches"] is True
        selected = set(r["kernel_selected_files"])
        truly = set(r["truly_matching_files"])
        assert truly <= selected, f"漏裁剪 seed={seed} i={i} preds={preds}"
