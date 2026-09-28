"""边界：多个层级向量信息损失并列（如单值列）时，最优应选最细层级。

回归测试：当提高层级只会合并"声明但样本未出现"的值时，各层级损失都为 0；
此时最优必须是枚举序最小（最细、层级 0）的可行向量，而不是最粗顶层。
"""

from __future__ import annotations

from app.models import DatasetIn
from app.core.anonymization import find_best_generalization
from app.core.logging_setup import StepLogger
from app.core.parsing import parse_dataset
from tests.reference_oracle import brute_force_optimum


def _payload(k=2, l=1):
    # QI 列样本中只有一个真实值 a；层级可以"变粗"但不改变样本分组
    columns = [
        {
            "name": "q",
            "role": "quasi_identifier",
            "hierarchy": {
                "levels": [
                    {"a": "a", "b": "b"},
                    {"a": "G", "b": "G"},
                ]
            },
        },
        {"name": "s", "role": "sensitive"},
    ]
    rows = [{"q": "a", "s": "x"}, {"q": "a", "s": "y"}]
    return DatasetIn(name="tie", columns=columns, rows=rows, k=k, l=l)


def test_tie_prefers_finest_level(settings):
    ds = parse_dataset(_payload(), settings)
    res = find_best_generalization(ds, StepLogger())
    assert res.status == "succeeded"
    assert res.best.levels == (0,)          # 不是 (1,)
    assert res.best.loss == 0.0
    assert res.best.classes[0].size == 2


def test_tie_matches_independent_oracle(settings):
    fixture = {
        "columns": [
            {
                "name": "q",
                "role": "quasi_identifier",
                "hierarchy": {
                    "levels": [{"a": "a", "b": "b"}, {"a": "G", "b": "G"}]
                },
            },
            {"name": "s", "role": "sensitive"},
        ],
        "rows": [{"q": "a", "s": "x"}, {"q": "a", "s": "y"}],
    }
    brute = brute_force_optimum(fixture, 2, 1)
    assert brute.optimum["vec"] == (0,)
