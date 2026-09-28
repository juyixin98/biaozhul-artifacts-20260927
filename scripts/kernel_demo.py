#!/usr/bin/env python3
"""不经过 Web，直接使用安全内核的示例：解析 -> 层级校验 -> 穷举建议。

::

    PYTHONPATH=src python scripts/kernel_demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from anon_risk.kernel import equivalence as eq       # noqa: E402
from anon_risk.kernel import optimizer               # noqa: E402
from anon_risk.kernel.hierarchy import (             # noqa: E402
    apply_vector, materialize,
)
from anon_risk.kernel.parser import parse_dataset    # noqa: E402


def main() -> None:
    payload = json.loads(
        (Path(__file__).resolve().parent.parent / "fixtures" / "tiny.json")
        .read_text(encoding="utf-8")
    )
    dataset = parse_dataset(payload)
    mats = {
        c: materialize(dataset.hierarchies[c], [r[c] for r in dataset.rows])
        for c in dataset.qi_columns
    }

    baseline_keys = apply_vector(
        dataset.rows, dataset.qi_columns, mats,
        {c: 0 for c in dataset.qi_columns},
    )
    base = eq.evaluate(dataset, baseline_keys,
                       {c: 0 for c in dataset.qi_columns}, 2, 2)
    print(f"基线：{len(base.classes)} 个等价类，"
          f"违规行={base.rows_in_violating_classes}，"
          f"最大检察官风险={base.worst_prosecutor_risk}")

    sug = optimizer.suggest(dataset, mats, 2, 2)
    print(f"建议：{sug.status} levels={sug.levels} "
          f"类大小={sug.class_sizes} DM={sug.discernibility} LM={sug.loss_metric}")
    print(f"依据：{sug.verdict_basis}")
    print()
    print("注意：", eq.DISCLAIMER)


if __name__ == "__main__":
    main()
