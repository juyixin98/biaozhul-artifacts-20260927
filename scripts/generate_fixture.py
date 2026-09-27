"""生成本地合成夹具 samples/fixture.json（确定性，无外部依赖）。

参考答案（expected）用**普通 Python 集合运算**从原始词项映射推导，
不经过被测的 postings 核心算子，保证测试预言独立于实现。
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.abspath(os.path.join(HERE, "..", "samples", "fixture.json"))

BLOCK_SIZE = 8

# 1..40 的有限文档全集
UNIVERSE = list(range(1, 41))

TERMS = {
    # 稀疏 posting（5 个文档，跨多个 8 文档块）
    "cat": [1, 9, 17, 25, 33],
    # 稠密 posting（14 个文档，步长 3）
    "dog": [1, 4, 7, 10, 13, 16, 19, 22, 25, 28, 31, 34, 37, 40],
    "fish": [2, 3, 9, 10, 17, 18, 25, 26, 33, 34],
    "mid": [8, 9, 10, 11, 12, 13, 14, 15],
    "rare": [40],
    # 覆盖整个全集：用于全否定与 OR 覆盖短路
    "everything": list(range(1, 41)),
}

# 版本 3 的删除集：删除文档必须同步全集可见性（含两个 cat 文档与 rare）
DELETES_V3 = [9, 25, 40]


def _expected_queries(universe, terms):
    """独立集合代数预言：只用内建 set，不导入 app.postings。"""
    U = set(universe)
    cat, dog, fish = set(terms["cat"]), set(terms["dog"]), set(terms["fish"])
    rare, everything, mid = (
        set(terms["rare"]),
        set(terms["everything"]),
        set(terms["mid"]),
    )
    cases = {
        "cat AND dog": cat & dog,
        "cat AND NOT dog": cat - dog,
        "cat OR dog": cat | dog,
        "NOT cat": U - cat,
        "NOT everything": U - everything,
        "cat AND fish": cat & fish,
        "rare AND everything": rare & everything,
        # AND 短路：rare ∩ cat 立即为空，第三个操作数不应被求值
        "rare AND cat AND dog": rare & cat & dog,
        # OR 覆盖短路：everything 已覆盖全集，cat 不应被求值
        "everything OR cat": everything | cat,
        # 执行顺序一致性的三操作数用例
        "cat AND dog AND fish": cat & dog & fish,
        "cat OR dog OR fish": cat | dog | fish,
        # 双重否定 = 自身
        "NOT NOT cat": U - (U - cat),
        # 否定与并/交混合
        "(cat OR dog) AND NOT fish": (cat | dog) - fish,
        "NOT cat AND NOT dog": (U - cat) & (U - dog),
        # 块跳过的构造性用例：稀疏 ∩ 稠密
        "mid AND cat": mid & cat,
    }
    return {q: sorted(ids) for q, ids in cases.items()}


def main() -> None:
    visible_v3 = sorted(set(UNIVERSE) - set(DELETES_V3))
    # 删除后变空的词项（如 rare）仍是“已知词项、空 posting”，
    # 而不是“未知词项”：保留空列表，区别于从未索引过的词。
    terms_v3 = {
        term: sorted(set(docs) & set(visible_v3)) for term, docs in TERMS.items()
    }

    fixture = {
        "block_size": BLOCK_SIZE,
        "initial_version": 1,
        "versions": [
            {"version_id": 1, "note": "初始空版本：空全集", "universe": [], "terms": {}},
            {
                "version_id": 2,
                "note": "完整合成语料",
                "universe": UNIVERSE,
                "terms": TERMS,
            },
            {
                "version_id": 3,
                "note": "在版本 2 基础上删除文档，验证删除同步全集可见性",
                "parent_version": 2,
                "deletes": DELETES_V3,
                "universe": visible_v3,
                "terms": terms_v3,
            },
        ],
        "expected_v2": _expected_queries(UNIVERSE, TERMS),
        "expected_v3": _expected_queries(visible_v3, terms_v3),
        "expected_v1": {
            # v1 是显式的空全集：任何有限补集都是空集（不扩展为无限整数集）
            "NOT cat": [],
            "NOT everything": [],
            "cat AND dog": [],
            "cat OR dog": [],
            # 注：v1 中不存在任何词项，这些查询以 unknown_terms_empty=true
            # 按空 posting 求值，结果必须仍为空全集。
            "unknown_terms_empty": True,
        },
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(fixture, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"wrote {OUT}")
    print(f"  v2 universe={len(UNIVERSE)} terms={len(TERMS)} expected_queries={len(fixture['expected_v2'])}")
    print(f"  v3 deletes={DELETES_V3} visible={len(visible_v3)}")


if __name__ == "__main__":
    main()
