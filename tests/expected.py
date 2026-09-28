"""手工推导的期望值（不来自被测代码）。

tiny 夹具（tests/oracle.py 里有同样数据的独立分层表）：

行：10001/23/Flu, 10002/25/Cold, 10003/31/Flu,
    12001/35/Cold, 12002/40/Flu, 12003/42/Cold

zip 层级深度 3：L0 原值；L1 前4位 {1000,1200}；L2 前2位 {10,12}；L3 {1x}
age 层级深度 2：L0 原值；L1 {[0,30),[30,50)}；L2 {[0,120)}

12 行判定表逐组手算登记；测试同时用 ``oracle.py`` 的独立暴力实现交叉验证，
任何一处手算错误都会被测试自身抓到。
"""

from __future__ import annotations

# (zip_level, age_level) -> 判定。键要点：
# zip 仍为 L0 时 6 个 zip 互不相同，age 怎么合都还是 6 个单例。
TINY_VECTOR_TABLE_K2_L2 = {
    (0, 0): {"sizes": [1, 1, 1, 1, 1, 1], "feasible": False, "dm": 6,
             "k_ok": False, "l_ok": False},
    (0, 1): {"sizes": [1, 1, 1, 1, 1, 1], "feasible": False, "dm": 6,
             "k_ok": False, "l_ok": False},
    (0, 2): {"sizes": [1, 1, 1, 1, 1, 1], "feasible": False, "dm": 6,
             "k_ok": False, "l_ok": False},
    (1, 0): {"sizes": [1, 1, 1, 1, 1, 1], "feasible": False, "dm": 6,
             "k_ok": False, "l_ok": False},
    # zip4 × age-bin：1000/<30={p1,p2}, 1000/30+={p3}, 1200/30+={p4,p5,p6}
    (1, 1): {"sizes": [3, 2, 1], "feasible": False, "dm": 14,
             "k_ok": False, "l_ok": False},
    (1, 2): {"sizes": [3, 3], "feasible": True, "dm": 18,
             "k_ok": True, "l_ok": True},
    (2, 0): {"sizes": [1, 1, 1, 1, 1, 1], "feasible": False, "dm": 6,
             "k_ok": False, "l_ok": False},
    (2, 1): {"sizes": [3, 2, 1], "feasible": False, "dm": 14,
             "k_ok": False, "l_ok": False},
    (2, 2): {"sizes": [3, 3], "feasible": True, "dm": 18,
             "k_ok": True, "l_ok": True},
    (3, 0): {"sizes": [1, 1, 1, 1, 1, 1], "feasible": False, "dm": 6,
             "k_ok": False, "l_ok": False},
    (3, 1): {"sizes": [4, 2], "feasible": True, "dm": 20,
             "k_ok": True, "l_ok": True},
    (3, 2): {"sizes": [6], "feasible": True, "dm": 36,
             "k_ok": True, "l_ok": True},
}

# 最优（k=2,l=2）：可行向量 (1,2),(2,2),(3,1),(3,2)，DM 18/18/20/36；
# DM=18 平局中 (1,2) 的归一化深度 LM 更小：(1/3+2/2)/2 = 2/3
TINY_OPTIMUM_K2_L2 = {
    "levels": {"zip": 1, "age": 2},
    "levels_tuple": [1, 2],
    "class_sizes": [3, 3],
    "class_count": 2,
    "discernibility": 18,
    "loss_metric": round((1 / 3 + 2 / 2) / 2, 10),
    "evaluated_vectors": 12,
    "total_vectors": 12,
    "feasible_vectors": [(1, 2), (2, 2), (3, 1), (3, 2)],
}

# 两个最优类各自的敏感值分布（次数 hist 与 distinct，非 NULL）
TINY_OPTIMUM_CLASS_DETAILS = [
    {"size": 3, "sensitive_distinct_non_null": 2,
     "sensitive_frequency_histogram": {1: 1, 2: 1},  # 一种病出现2次、另一种1次
     "sensitive_null_members": 0},
    {"size": 3, "sensitive_distinct_non_null": 2,
     "sensitive_frequency_histogram": {1: 1, 2: 1},
     "sensitive_null_members": 0},
]

# k=2,l=3：全合并后敏感值 distinct=2 < 3 => 阈值不可达（明确结论，非错误）
TINY_K2_L3_UNREACHABLE = {
    "distinct_non_null_sensitive_values": 2,
    "full_generalization_l_ok": False,
    "l": 3,
}

# k=7 超过行数 6 => 全泛化最大类只有 6 人，k 不可达
TINY_K7_UNREACHABLE = {
    "row_count": 6,
    "k": 7,
    "full_generalization_min_class": 6,
}
