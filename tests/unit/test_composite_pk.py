"""复合主键测试：记录身份由多列共同决定。"""
from __future__ import annotations

import json

from merge3.domain.models import FieldSpec, TableSpec
from merge3.kernel import diff3


SPEC = TableSpec(
    name="kv",
    primary_key=["tenant", "seq"],
    fields=[FieldSpec("tenant", "string", False), FieldSpec("seq", "int64", False),
            FieldSpec("note", "string")],
)


def test_composite_pk_identity_and_conflict():
    base = [{"tenant": "a", "seq": 1, "note": "x"},
            {"tenant": "b", "seq": 1, "note": "y"}]
    ours = [{"tenant": "a", "seq": 1, "note": "X"},   # 改 (a,1)
            {"tenant": "b", "seq": 1, "note": "y"}]
    theirs = [{"tenant": "a", "seq": 1, "note": "x"},
              {"tenant": "b", "seq": 1, "note": "Y"},  # 改 (b,1)
              {"tenant": "a", "seq": 2, "note": "z"}]  # 新增 (a,2)
    p = diff3.build_plan(SPEC, base, ours, theirs, "b", "o", "t")
    keys = {tuple(json.loads(k)): e for k, e in p.entries.items()}
    assert keys[("a", 1)].classification == "ours_modified"
    assert keys[("b", 1)].classification == "theirs_modified"
    assert keys[("a", 2)].classification == "theirs_added"
    assert not p.conflicts
    merged = {tuple(e.key.values()): e.merged["note"]
              for e in p.entries.values() if not e.deleted}
    assert merged == {("a", 1): "X", ("b", 1): "Y", ("a", 2): "z"}


def test_composite_pk_same_seq_different_tenant_is_not_conflict():
    # seq 相同但 tenant 不同：不是同一条记录，互不冲突
    base: list = []
    ours = [{"tenant": "a", "seq": 1, "note": "from-a"}]
    theirs = [{"tenant": "b", "seq": 1, "note": "from-b"}]
    p = diff3.build_plan(SPEC, base, ours, theirs, "b", "o", "t")
    assert set(p.conflicts) == set()
    assert len(p.entries) == 2
