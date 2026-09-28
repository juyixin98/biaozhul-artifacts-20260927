"""黄金期望核对：把回放 trace 与人工编写的期望逐事件比对。

期望文件格式（JSON，人工编写——不由被测内核生成）::

    {
      "scenario": "fees-nonces",
      "expect": [
        {"index": 2, "ok": true,  "result": {"status": "queued", "reason": "NONCE_GAP"}},
        {"index": 5, "ok": false, "result": {"error": "REPLACEMENT_UNDERPRICED"}},
        {"snapshot_after_index": 12,
         "counts": {"pending": 3, "mined": 2},
         "accounts": [{"address": "alice", "balance": "...", "nonce": 2}]}
      ]
    }

匹配规则：
* 事件条目只比对 ``result`` 中**列出的键**（子集匹配），便于人工聚焦关键结果；
* snapshot 条目在指定事件跑完后取一次内核快照比对（accounts 可用标签地址）；
* 任何期望未满足都抛 :class:`GoldenMismatch`，消息列出全部差异（不是只报第一个）。
"""

from __future__ import annotations

import json
from pathlib import Path

from .runner import Replayer


class GoldenMismatch(AssertionError):
    pass


def _subset_equal(expected, actual, path: str, diffs: list[str]) -> None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            diffs.append(f"{path}: expected dict, got {type(actual).__name__}")
            return
        for k, v in expected.items():
            if k not in actual:
                diffs.append(f"{path}.{k}: missing in actual")
            else:
                _subset_equal(v, actual[k], f"{path}.{k}", diffs)
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            diffs.append(f"{path}: list length differs: expect {len(expected)} "
                         f"got {len(actual) if isinstance(actual, list) else type(actual)}")
            return
        for i, (e, a) in enumerate(zip(expected, actual)):
            _subset_equal(e, a, f"{path}[{i}]", diffs)
    else:
        # 数字/字符串比较前把"看起来相等"的数值归一化（wei 以字符串存储）
        if str(expected) != str(actual):
            diffs.append(f"{path}: expect {expected!r} got {actual!r}")


def verify(replayer: Replayer, golden_path: str | Path) -> dict:
    golden = json.loads(Path(golden_path).read_text(encoding="utf-8"))
    traces = {t.index: t for t in replayer.traces}
    labels = replayer.accounts
    diffs: list[str] = []
    matched = 0

    for item in golden.get("expect", []):
        if "snapshot_after_index" in item:
            idx = item["snapshot_after_index"]
            if idx not in traces:
                diffs.append(f"snapshot_after_index={idx}: event never ran")
                continue
            # 快照必须在指定事件**跑完的那一刻**记录，而不是全部回放完之后
            # （否则断言的就是错误时间点的状态）。
            snap = replayer.snapshots.get(idx)
            if snap is None:
                # 允许挂在任意事件后：运行时无法预知，退化为缺失
                diffs.append(f"snapshot_after_index={idx}: event is not a snapshot marker; "
                             "add a snapshot event at that index")
                continue
            if "counts" in item:
                _subset_equal(item["counts"], snap["counts"],
                              f"event#{idx}.counts", diffs)
            if "accounts" in item:
                by_addr = {a["address"]: a for a in snap["accounts"]}
                for exp_acct in item["accounts"]:
                    label = exp_acct["address"]
                    addr = labels.get(label, {}).get("address", label)
                    actual = by_addr.get(addr)
                    if actual is None:
                        diffs.append(f"event#{idx}.accounts[{label}] ({addr}): missing")
                    else:
                        _subset_equal({k: v for k, v in exp_acct.items() if k != "address"},
                                      actual, f"event#{idx}.accounts[{label}]", diffs)
            if "transactions" in item:
                by_hash = {t["hash"]: t for t in snap["transactions"]}
                for exp_tx in item["transactions"]:
                    key = exp_tx.get("label") or exp_tx.get("hash")
                    if "label" in exp_tx:
                        # 标签 -> 在 trace 中找 tx 事件产出的 hash
                        label = exp_tx.pop("label")
                        key = _tx_label_to_hash(traces, label)
                        if key is None:
                            diffs.append(f"event#{idx}.tx[{label}]: label never produced a tx")
                            continue
                    actual = by_hash.get(key)
                    if actual is None:
                        diffs.append(f"event#{idx}.tx[{key}]: not present in snapshot")
                    else:
                        _subset_equal({k: v for k, v in exp_tx.items() if k not in ("hash",)},
                                      actual, f"event#{idx}.tx[{key}]", diffs)
            if "head_number" in item:
                _subset_equal(item["head_number"], snap["head_number"],
                              f"event#{idx}.head_number", diffs)
            matched += 1
            continue

        idx = item["index"]
        trace = traces.get(idx)
        if trace is None:
            diffs.append(f"event#{idx}: never ran")
            continue
        if "ok" in item and item["ok"] != trace.ok:
            diffs.append(f"event#{idx} ({trace.type}): ok expect {item['ok']} got {trace.ok} "
                         f"(actual result={json.dumps(trace.result, ensure_ascii=False)})")
        if "result" in item:
            _subset_equal(item["result"], trace.result, f"event#{idx}.result", diffs)
        matched += 1

    if diffs:
        raise GoldenMismatch(
            f"golden {golden_path} failed with {len(diffs)} mismatch(es):\n  - "
            + "\n  - ".join(diffs))
    return {"golden": golden.get("scenario", str(golden_path)),
            "matched_expectations": matched, "traces": len(traces), "diffs": []}


def _tx_label_to_hash(traces: dict, label: str) -> str | None:
    """tx 事件可在事件文件中带 ``label``，trace.result 也会保留 label。"""
    for t in traces.values():
        if t.type in ("tx",) and t.result.get("label") == label:
            return t.result["tx_hash"]
    return None
