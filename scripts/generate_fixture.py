#!/usr/bin/env python3
"""合成数据生成器：本地确定性生成小表与对应层级声明。

用法::

    python scripts/generate_fixture.py --n 30 --seed 7 --out /tmp/synth.json

所有取值都来自内置合成分布（地区前缀/年龄段/诊断标签），不含真实业务数据。
随机数用固定种子，输出可复现。
"""

from __future__ import annotations

import argparse
import json
import random

REGIONS = ["100", "120", "200", "310"]
DIAGNOSES = ["Flu", "Cold", "Asthma", "Gastritis", "Migraine"]


def generate(n: int, seed: int) -> dict:
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        region = rng.choice(REGIONS)
        tail = rng.randint(0, 99)
        zip_code = f"{region}{tail:02d}"
        age = rng.randint(18, 90)
        # 故意注入少量 NULL，验证缺失值不会被丢行
        disease = rng.choice(DIAGNOSES) if rng.random() > 0.08 else ""
        rows.append([zip_code, str(age), disease])

    return {
        "columns": ["zip", "age", "disease"],
        "quasi_identifiers": ["zip", "age"],
        "sensitive": ["disease"],
        "rows": rows,
        "hierarchies": {
            "zip": {
                "levels": [
                    {"rule": "prefix", "keep": 4, "name": "zip4"},
                    {"rule": "prefix", "keep": 3, "name": "region3"},
                ]
            },
            "age": {
                "levels": [
                    {"rule": "range", "bins": [0, 30, 45, 60, 200],
                     "labels": ["<30", "30-44", "45-59", "60+"],
                     "name": "band"},
                    {"rule": "range", "bins": [0, 200],
                     "labels": ["any_age"], "name": "any"},
                ]
            },
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    payload = generate(args.n, args.seed)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"已写入 {args.out}（{args.n} 行，seed={args.seed}）")
    else:
        print(text)


if __name__ == "__main__":
    main()
