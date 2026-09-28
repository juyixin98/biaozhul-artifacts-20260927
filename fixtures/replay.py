"""通过 HTTP 重放夹具场景，并把服务输出归一化到与手写期望/oracle 可比对的形状。

三方对照：
1. 手写期望：scenario JSON 中每版本的 expect（人工编写）；
2. 独立 oracle：app.kernel.oracle 纯标准库重放；
3. 被测系统：对 FastAPI 发真实 HTTP 请求（TestClient/ASGI），读 Parquet+SQLite。
任何两方不一致都以明确的失败类别抛出（ReplayFailure），绝不只检查“接口能调用”。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.kernel.oracle import OracleState
from fixtures import Scenario


class ReplayFailure(AssertionError):
    """带类别的断言失败：category 标识失败归属（EXPECTATION/ORACLE/API/SEQUENCE ...）。"""

    def __init__(self, category: str, message: str, detail: dict[str, Any] | None = None):
        super().__init__(f"[{category}] {message}")
        self.category = category
        self.detail = detail or {}


@dataclass
class VersionRun:
    seq: int
    snapshot_id: str | None
    status: str                      # OK / EXPECTED_ERROR
    run_ids: list[str] = field(default_factory=list)
    service_rows: list[dict[str, Any]] = field(default_factory=list)
    oracle_version: Any = None
    ref_map: dict[str, str] = field(default_factory=dict)


@dataclass
class ReplayResult:
    scenario_name: str
    table_id: str
    versions: list[VersionRun] = field(default_factory=list)
    # seq -> VersionRun（错误版本不产生快照，沿用上一 seq）
    ref_map_global: dict[str, str] = field(default_factory=dict)
    snapshot_by_seq: dict[int, str] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)


def replay_scenario(client, scenario: Scenario) -> ReplayResult:
    """重放整个场景并完成全部断言；失败抛 ReplayFailure。返回最终比对轨迹。"""
    # 1) 建表
    r = client.post("/tables", json={
        "name": scenario.name,
        "columns": scenario.schema["columns"],
        "primary_key": scenario.schema["primary_key"],
    })
    _assert_status(r, 200, "create_table")
    table_id = r.json()["table_id"]

    result = ReplayResult(scenario_name=scenario.name, table_id=table_id)
    oracle = OracleState(
        name=scenario.name,
        columns=scenario.schema["columns"],
        primary_key=scenario.schema["primary_key"],
    )

    parent: str | None = None
    seq = 0
    for v_index, version in enumerate(scenario.versions, start=1):
        operations = _resolve_refs(version["operations"], result.ref_map_global)
        body = {"table_id": table_id, "parent_snapshot_id": parent, "operations": operations}
        r = client.post(f"/tables/{table_id}/commits", json=body)
        run = VersionRun(seq=seq, snapshot_id=None, status="", run_ids=[r.json().get("run_id")])

        if "expect_error" in version:
            exp_err = version["expect_error"]
            if r.status_code == 200:
                raise ReplayFailure(
                    "ERROR_CATEGORY",
                    f"v{v_index} expected {exp_err['category']}/{exp_err['code']} but commit succeeded",
                    {"note": version.get("note")},
                )
            err = r.json()["error"]
            if err["category"] != exp_err["category"] or err["code"] != exp_err["code"]:
                raise ReplayFailure(
                    "ERROR_CATEGORY",
                    f"v{v_index} error mismatch: expected {exp_err['category']}/{exp_err['code']}, "
                    f"got {err['category']}/{err['code']}",
                    {"note": version.get("note"), "actual": err},
                )
            run.status = "EXPECTED_ERROR"
            run.run_ids.append(r.json().get("run_id"))
            result.versions.append(run)
            # 错误版本不得改变后续父快照/序列号
            continue

        _assert_status(r, 200, f"commit v{v_index}")
        payload = r.json()
        seq = payload["seq"]
        parent = payload["snapshot_id"]
        run.seq = seq
        run.snapshot_id = parent
        run.status = "OK"
        result.snapshot_by_seq[seq] = parent
        for f in payload.get("files", []):
            result.ref_map_global[f["ref"]] = f["file_id"]
        run.ref_map = dict(result.ref_map_global)

        # 2) oracle 独立重放同一版本
        try:
            over = oracle.commit(version["operations"])
        except ValueError as exc:
            raise ReplayFailure("ORACLE", f"v{v_index} oracle rejected valid operations: {exc}")
        run.oracle_version = over

        # 3) 服务 explain 全量逐行
        er = client.post(f"/tables/{table_id}/explain", json={"snapshot_id": parent})
        _assert_status(er, 200, f"explain v{v_index}")
        run.run_ids.append(er.json().get("run_id"))
        service = _service_dispositions(er.json())
        run.service_rows = service

        # 4) 三方比对
        oracle_disp = _oracle_dispositions(over, result.ref_map_global)
        _compare_rows(
            service, oracle_disp,
            label=f"v{v_index} service-vs-oracle", note=version.get("note"),
            compare_reasons=True,
        )
        if "expect" in version:
            hand = _hand_dispositions(version["expect"], result.ref_map_global)
            _compare_rows(
                service, hand, label=f"v{v_index} service-vs-handwritten",
                note=version.get("note"), compare_reasons=True,
            )
            _compare_files(er.json()["data_files"], version["expect"].get("files"),
                           result.ref_map_global, f"v{v_index}")
            _oracle_vs_hand(over, version["expect"], result.ref_map_global, v_index)
        result.versions.append(run)

    # 5) 时间旅行读取
    for read_case in scenario.reads:
        snap = result.snapshot_by_seq.get(read_case["version"])
        if snap is None:
            raise ReplayFailure("SEQUENCE", f"read case references missing version {read_case['version']}")
        rr = client.get(f"/tables/{table_id}/rows", params={"snapshot_id": snap})
        _assert_status(rr, 200, f"read@{read_case['version']}")
        got = [
            {"file_id": d["file_id"], "position": d["position"], "values": row}
            for d, row in zip(rr.json()["row_drivers"], rr.json()["rows"])
        ]
        expected = [
            {"file_id": result.ref_map_global[e["file_ref"]], "position": e["position"],
             "values": e["values"]}
            for e in read_case["live"]
        ]
        _compare_live(got, expected, label=f"read@v{read_case['version']} ({read_case.get('note')})")

    # 6) explain 过滤用例（服务 vs oracle 过滤结果）
    for ec in scenario.explain_cases:
        er = client.post(f"/tables/{table_id}/explain",
                         json={"filter": ec["filter"], "include_filtered": True})
        _assert_status(er, 200, "explain filter case")
        service = _service_dispositions(er.json())
        last = oracle.versions[-1]
        of = oracle.filtered_expectation(last, filter=ec["filter"])
        oracle_flat = _oracle_filtered(of, result.ref_map_global)
        _compare_rows(service, oracle_flat, label=f"explain-case {ec['note']}",
                      compare_reasons=False)
        hand = _hand_dispositions({"dispositions": ec["expect"]}, result.ref_map_global)
        _compare_rows(service, hand, label=f"explain-case handwritten {ec['note']}",
                      compare_reasons=False)

    # 7) rows（投影+过滤）用例
    for rc in scenario.rows_cases:
        params: dict[str, Any] = {}
        if rc.get("columns"):
            params["columns"] = ",".join(rc["columns"])
        if rc.get("filter"):
            params["filter"] = json.dumps(rc["filter"])
        rr = client.get(f"/tables/{table_id}/rows", params=params)
        _assert_status(rr, 200, "rows case")
        got = [
            {"file_id": d["file_id"], "position": d["position"], "values": row}
            for d, row in zip(rr.json()["row_drivers"], rr.json()["rows"])
        ]
        expected = [
            {"file_id": result.ref_map_global[e["file_ref"]], "position": e["position"],
             "values": e["values"]}
            for e in rc["expect"]
        ]
        _compare_live(got, expected, label=f"rows-case {rc['note']}")

    ev = client.get(f"/tables/{table_id}/events")
    result.events = ev.json()["events"]
    return result


# ---------------------------------------------------------------------------
# 比对辅助
# ---------------------------------------------------------------------------
def _resolve_refs(operations: list[dict[str, Any]], ref_map: dict[str, str]) -> list[dict[str, Any]]:
    """把操作中的 ref 目标翻译成已分配的服务端 file_id（仅 target_file/drops）。"""
    resolved = json.loads(json.dumps(operations))
    for op in resolved:
        if op.get("op") == "position_delete" and op["target_file"] in ref_map:
            op["target_file"] = ref_map[op["target_file"]]
        if op.get("op") == "rewrite":
            op["drops"] = [ref_map.get(d, d) for d in op["drops"]]
    return resolved


def _assert_status(r, expected: int, where: str) -> None:
    if r.status_code != expected:
        raise ReplayFailure(
            "API", f"{where}: expected HTTP {expected}, got {r.status_code}",
            {"body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text},
        )


def _key(r: dict[str, Any]) -> tuple[str, int]:
    return r["file_id"], r["position"]


def _service_dispositions(explain: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in explain["rows"]:
        out.append({
            "file_id": r["file_id"],
            "position": r["position"],
            "added_seq": r["added_seq"],
            "disposition": r["disposition"],
            "reasons": [_norm_reason(x) for x in r["reasons"]],
        })
    out.sort(key=_key)
    return out


def _norm_reason(reason: dict[str, Any]) -> dict[str, Any]:
    # 三方统一到 {kind, seq}（等值删除附带 key 时保留）
    out = {"kind": reason["kind"], "seq": reason["seq"]}
    if "key" in reason:
        out["key"] = reason["key"]
    return out


def _oracle_dispositions(version, ref_map: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    for d in version.dispositions:
        out.append({
            "file_id": ref_map[d["file_ref"]],
            "position": d["position"],
            "added_seq": d["added_seq"],
            "disposition": "DELETED" if d["deleted"] else "KEPT",
            "reasons": [_norm_reason(r) for r in d["reasons"]],
        })
    out.sort(key=_key)
    return out


def _oracle_filtered(filtered: dict[str, Any], ref_map: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    for bucket, disp in (("kept", "KEPT"), ("deleted", "DELETED"), ("filtered", "FILTERED")):
        for d in filtered[bucket]:
            out.append({
                "file_id": ref_map[d["file_ref"]],
                "position": d["position"],
                "added_seq": d["added_seq"],
                "disposition": disp,
                "reasons": [_norm_reason(r) for r in d["reasons"]],
            })
    out.sort(key=_key)
    return out


def _hand_dispositions(expect: dict[str, Any], ref_map: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    for d in expect.get("dispositions", []):
        out.append({
            "file_id": ref_map[d["file_ref"]],
            "position": d["position"],
            "disposition": d["disposition"],
            "reasons": [_norm_reason(r) for r in d.get("reasons", [])],
        })
    out.sort(key=_key)
    return out


def _compare_rows(
    got: list[dict[str, Any]], expected: list[dict[str, Any]], *,
    label: str, note: str | None = None, compare_reasons: bool,
) -> None:
    g = {_key(r): r for r in got}
    e = {_key(r): r for r in expected}
    if set(g) != set(e):
        raise ReplayFailure(
            "RESULT_SET_MISMATCH",
            f"{label}: row identity sets differ",
            {"note": note, "missing": sorted(set(e) - set(g)), "unexpected": sorted(set(g) - set(e))},
        )
    for k in sorted(g):
        gr, er = g[k], e[k]
        if gr["disposition"] != er["disposition"]:
            raise ReplayFailure(
                "DISPOSITION_MISMATCH",
                f"{label}: {k} disposition expected {er['disposition']}, got {gr['disposition']}",
                {"note": note},
            )
        if compare_reasons and _reasons_sig(gr["reasons"]) != _reasons_sig(er["reasons"]):
            raise ReplayFailure(
                "REASON_MISMATCH",
                f"{label}: {k} reasons expected {er['reasons']}, got {gr['reasons']}",
                {"note": note},
            )


def _reasons_sig(reasons: list[dict[str, Any]]) -> list[tuple[str, int]]:
    # 手写期望按 seq 排序；同一行多理由以 (kind, seq) 多重集比较
    return sorted((r["kind"], r["seq"]) for r in reasons)


def _compare_files(actual_files: list[str], expected_refs: list[str] | None,
                   ref_map: dict[str, str], label: str) -> None:
    if expected_refs is None:
        return
    expected = sorted(ref_map[r] for r in expected_refs)
    if sorted(actual_files) != expected:
        raise ReplayFailure(
            "FILE_SET_MISMATCH", f"{label}: live file sets differ",
            {"expected": expected, "actual": sorted(actual_files)},
        )


def _oracle_vs_hand(oracle_version, expect: dict[str, Any], ref_map: dict[str, str], v_index: int) -> None:
    """手写期望与 oracle 也要彼此一致（防止测试与被测代码同源错误）。"""
    oracle_set = {
        (ref_map[d["file_ref"]], d["position"]): ("DELETED" if d["deleted"] else "KEPT")
        for d in oracle_version.dispositions
    }
    hand_set = {
        (ref_map[d["file_ref"]], d["position"]): d["disposition"]
        for d in expect.get("dispositions", [])
    }
    if oracle_set != hand_set:
        raise ReplayFailure(
            "ORACLE_VS_HANDWRITTEN",
            f"v{v_index}: independent oracle disagrees with handwritten expectations",
            {"oracle": oracle_set, "handwritten": hand_set},
        )


def _compare_live(got: list[dict[str, Any]], expected: list[dict[str, Any]], *, label: str) -> None:
    g = sorted(got, key=lambda r: _key(r))
    e = sorted(expected, key=lambda r: _key(r))
    if len(g) != len(e) or [_key(r) for r in g] != [_key(r) for r in e]:
        raise ReplayFailure("RESULT_SET_MISMATCH", f"{label}: live row identity differs",
                            {"expected_keys": [_key(r) for r in e], "got_keys": [_key(r) for r in g]})
    for gr, er in zip(g, e):
        if gr["values"] != er["values"]:
            raise ReplayFailure("VALUE_MISMATCH", f"{label}: value mismatch at {_key(gr)}",
                                {"expected": er["values"], "got": gr["values"]})
