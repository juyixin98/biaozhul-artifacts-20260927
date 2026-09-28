#!/usr/bin/env python3
"""独立复核 fixtures/scenarios.json 中的手算期望值。

参考实现与 Rust 内核零共享：
  - 点状态存 dict[(x, y)] -> int（Python int 任意精度）；
  - 矩形和 = 遍历点表全扫描，闭区间判定；
  - 注册坐标用 set（自己排序去重）；
  - 拒绝类别按与服务端相同的规则独立判定。

退出码 0 表示夹具中每个 expected_sum / expected_empty / expected_code /
expected_register / expected_version 都与独立计算一致；非 0 表示夹具本身有错。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

I64_MIN = -(2**63)
I64_MAX = 2**63 - 1

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "scenarios.json"


class RefStore:
    def __init__(self, xs: list[int], ys: list[int]):
        if not xs:
            raise ValueError("EMPTY_COORDINATES:x")
        if not ys:
            raise ValueError("EMPTY_COORDINATES:y")
        self.xs = set(xs)
        self.ys = set(ys)
        self.history: list[dict[tuple[int, int], int]] = [{}]

    def commit(
        self, updates: list[dict], base: int | None = None
    ) -> tuple[str | None, int | None]:
        """返回 (error_code_or_None, new_version_or_None)。

        base 语义独立实现：显式基版本不存在 → VERSION_NOT_FOUND；
        存在但不是最新 → STALE_BASE_VERSION；None 表示基于最新。
        """
        if not updates:
            return "EMPTY_BATCH", None
        current = len(self.history) - 1
        if base is not None:
            if base < 0 or base > current:
                return "VERSION_NOT_FOUND", None
            if base != current:
                return "STALE_BASE_VERSION", None
        for u in updates:
            if u["x"] not in self.xs or u["y"] not in self.ys:
                return "COORDINATE_NOT_REGISTERED", None
        agg: dict[tuple[int, int], int] = {}
        for u in updates:
            agg[(u["x"], u["y"])] = agg.get((u["x"], u["y"]), 0) + u["delta"]
        nxt = dict(self.history[-1])
        for (x, y), d in agg.items():
            nv = nxt.get((x, y), 0) + d
            if nv < I64_MIN or nv > I64_MAX:
                return "POINT_OVERFLOW", None
            nxt[(x, y)] = nv
        self.history.append(nxt)
        return None, len(self.history) - 1

    def query(self, version: int, r: dict) -> tuple[str | None, int, bool]:
        if r["x_lo"] > r["x_hi"] or r["y_lo"] > r["y_hi"]:
            return "INVERTED_RECT", 0, False
        if version < 0 or version >= len(self.history):
            return "VERSION_NOT_FOUND", 0, False
        snap = self.history[version]
        xs_sel = sorted(x for x in self.xs if r["x_lo"] <= x <= r["x_hi"])
        ys_sel = set(y for y in self.ys if r["y_lo"] <= y <= r["y_hi"])
        empty = not xs_sel or not ys_sel
        total = sum(v for (x, y), v in snap.items() if x in xs_sel and y in ys_sel)
        return None, total, empty


def check_case(case: dict) -> list[str]:
    errs: list[str] = []
    name = case["name"]

    # 注册统计独立计算
    xs, ys = case["xs_register"], case["ys_register"]
    exp_reg = case["expected_register"]
    got = {
        "nx": len(set(xs)),
        "ny": len(set(ys)),
        "duplicate_x": len(xs) - len(set(xs)),
        "duplicate_y": len(ys) - len(set(ys)),
    }
    for k, want in exp_reg.items():
        if got[k] != want:
            errs.append(f"[{name}] register {k}: hand={want} independent={got[k]}")

    store = RefStore(xs, ys)

    def run_batch(item: dict, where: str) -> None:
        code, ver = store.commit(item["updates"], base=item.get("base_version"))
        want_code = item.get("expected_code")
        want_ver = item.get("expected_version")
        if want_code is not None and code != want_code:
            errs.append(
                f"[{name}] {where}: hand expects code {want_code}, independent gives {code}"
            )
        if want_ver is not None and ver != want_ver:
            errs.append(f"[{name}] {where}: hand expects version {want_ver}, independent gives {ver}")

    for i, b in enumerate(case.get("batches", [])):
        run_batch(b, f"batches[{i}]")

    for i, q in enumerate(case.get("queries", [])):
        code, total, empty = store.query(q["version"], q)
        if code is not None:
            errs.append(f"[{name}] queries[{i}] unexpectedly rejected by independent model: {code}")
            continue
        if total != q["expected_sum"]:
            errs.append(
                f"[{name}] queries[{i}] sum: hand={q['expected_sum']} independent={total} "
                f"(v{q['version']}, rect=[{q['x_lo']},{q['x_hi']}]x[{q['y_lo']},{q['y_hi']}])"
            )
        if empty != q["expected_empty"]:
            errs.append(
                f"[{name}] queries[{i}] empty: hand={q['expected_empty']} independent={empty}"
            )

    for i, item in enumerate(case.get("rejections", [])):
        if item["kind"] in ("batch", "success"):
            run_batch(item, f"rejections[{i}]")
        elif item["kind"] == "query":
            code, _, _ = store.query(item.get("version", len(store.history) - 1), item)
            if code != item["expected_code"]:
                errs.append(
                    f"[{name}] rejections[{i}] query code: hand={item['expected_code']} independent={code}"
                )

    for i, q in enumerate(case.get("post_rejection_queries", [])):
        code, total, empty = store.query(q["version"], q)
        if code is not None:
            errs.append(f"[{name}] post_rejection_queries[{i}] unexpected {code}")
            continue
        if total != q["expected_sum"] or empty != q["expected_empty"]:
            errs.append(
                f"[{name}] post_rejection_queries[{i}] hand=({q['expected_sum']},{q['expected_empty']}) "
                f"independent=({total},{empty})"
            )

    return errs


def main() -> int:
    doc = json.loads(FIXTURE.read_text())
    assert doc["schema"] == "pr2d-fixtures/v1", "unexpected fixture schema"
    all_errs: list[str] = []
    for case in doc["cases"]:
        all_errs.extend(check_case(case))
    if all_errs:
        print("FIXTURE CROSS-CHECK FAILED:")
        for e in all_errs:
            print("  -", e)
        return 1
    print(f"OK: {len(doc['cases'])} fixture cases agree with independent sparse full-scan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
