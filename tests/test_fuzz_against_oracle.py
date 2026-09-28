"""属性测试：随机生成 base/dev/main 三方行集，内核判定必须逐键等于独立 oracle。

参考答案（oracle）是另一份独立实现（tests/oracle.py），不由被测内核生成。
"""
from __future__ import annotations

import random

import pytest

from table_merge.merge_kernel import three_way_merge
from table_merge.models import key_string

from .conftest import EMP_COLUMNS
from .oracle import oracle_merge

CITIES = ["bj", "sh", "gz", "sz", "hz", None]
NAMES = ["a", "b", "c", None]


def _random_rows(rng: random.Random, ids: list[int]) -> list[dict]:
    rows = []
    for i in ids:
        rows.append({
            "id": i,
            "name": rng.choice(NAMES),
            "city": rng.choice(CITIES),
            "score": None if rng.random() < 0.2 else float(rng.randint(0, 5)),
            "active": rng.choice([True, False]),
        })
    return rows


@pytest.mark.parametrize("seed", range(40))
def test_fuzz_kernel_matches_oracle(seed, emp_schema):
    from table_merge.models import TableSchema
    schema = TableSchema.from_dict(emp_schema)
    rng = random.Random(seed)

    universe = list(range(1, 9))
    base_ids = rng.sample(universe, k=rng.randint(2, 6))
    base = _random_rows(rng, sorted(base_ids))

    def mutate(base_rows):
        rows = [dict(r) for r in base_rows]
        # 随机删除
        for r in list(rows):
            if rng.random() < 0.25:
                rows.remove(r)
        # 随机改字段
        for r in rows:
            if rng.random() < 0.5:
                field = rng.choice(["name", "city", "score", "active"])
                r[field] = rng.choice(NAMES if field == "name"
                                      else CITIES if field == "city"
                                      else [0.0, 1.0, 2.0] if field == "score"
                                      else [True, False])
        # 随机新增（可能两侧新增同主键）
        for i in universe:
            if all(r["id"] != i for r in rows) and rng.random() < 0.3:
                rows.extend(_random_rows(rng, [i]))
        return rows

    dev = mutate(base)
    main = mutate(base)

    report = three_way_merge(schema, base, dev, main)
    expected = oracle_merge(list(EMP_COLUMNS), ("id",), base, dev, main)

    assert {d.key for d in report.decisions} == set(expected)
    merged_index = {tuple(r[c] for c in schema.primary_key): r for r in report.merged_rows}
    for detail in report.decisions:
        exp_decision, exp_row = expected[detail.key]
        assert detail.decision.value == exp_decision, (
            f"seed={seed} key={key_string(detail.key)}: "
            f"kernel={detail.decision.value} oracle={exp_decision}"
        )
        if not detail.decision.is_conflict and exp_row is not None:
            assert merged_index[detail.key] == exp_row
        if not detail.decision.is_conflict and exp_row is None:
            assert detail.key not in merged_index  # 快进删除
