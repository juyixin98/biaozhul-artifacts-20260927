#!/usr/bin/env python3
"""独立端到端验证脚本（HTTP，真实启动 uvicorn 子进程）。

用途：不跑 pytest 也能验证服务边界。流程：
  启动服务 -> 建表 -> 载入 -> 删除 -> 查询 -> 重写 -> 再删后插 ->
  断言具体行结果与错误类别 -> 拉取 run 日志验证可重放 -> 关闭。

退出码 0 表示全部断言通过。用法：
    python scripts/verify_service.py [--workspace DIR] [--port 8011]
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def wait_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"服务未在 {timeout}s 内于 {port} 端口就绪")


class Checker:
    def __init__(self, base: str) -> None:
        self.http = httpx.Client(base_url=base, timeout=10)
        self.passed = 0
        self.failed: list[str] = []

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed.append(f"{name} {detail}")
            print(f"  FAIL  {name} {detail}")

    def expect(self, name: str, resp: httpx.Response, status: int) -> dict:
        ok = resp.status_code == status
        self.check(name, ok, f"got {resp.status_code}: {resp.text[:200]}")
        return resp.json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=str(ROOT / "verify_ws"))
    ap.add_argument("--port", type=int, default=8011)
    args = ap.parse_args()

    env = {**os.environ, "DELETER_WORKSPACE": args.workspace,
           "PYTHONPATH": str(ROOT / "src")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "deleter.api.app:create_app",
         "--factory", "--host", "127.0.0.1", "--port", str(args.port)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_port(args.port)
        ck = Checker(f"http://127.0.0.1:{args.port}")
        h = ck.http

        ck.expect("health", h.get("/health"), 200)
        ck.expect("create table", h.post("/tables", json={
            "table_id": "v1",
            "columns": {"id": "int64", "name": "string"},
            "key": ["id"]}), 201)

        ck.expect("load f1", h.post("/tables/v1/load", json={
            "file_id": "f1", "source": {"kind": "inline", "rows": [
                {"id": 1, "name": "a"}, {"id": 2, "name": "b"},
                {"id": 2, "name": "b2"}, {"id": 3, "name": "c"}]}}), 201)
        ck.expect("load f2", h.post("/tables/v1/load", json={
            "file_id": "f2", "source": {"kind": "inline", "rows": [
                {"id": 4, "name": "d"}, {"id": None, "name": "z"}]}}), 201)

        # 等值删除：跨文件重复键 id=2 删两行
        r = ck.expect("equality delete id=2", h.post("/tables/v1/deletes", json={
            "deletes": [{"delete_id": "del2", "kind": "equality",
                         "key": {"id": 2}}]}), 200)
        ck.check("eq delete matched 2 rows",
                 r["result"]["results"][0]["matched_rows"] == [["f1", 1], ["f1", 2]],
                 str(r["result"]["results"][0]["matched_rows"]))

        # 位置删除 f1#3（id=3），绑定 f1 v1
        ck.expect("position delete f1#3", h.post("/tables/v1/deletes", json={
            "deletes": [{"delete_id": "p3", "kind": "position",
                         "file_id": "f1", "row_number": 3}]}), 200)

        q = ck.expect("query survivors", h.post("/tables/v1/query", json={}), 200)
        survivors = [(row["file_id"], row["row_number"], row["values"]["id"])
                     for row in q["result"]["rows"]]
        # id=1 在 f1#0 幸存；两个 id=2（f1#1,#2）与 id=3(f1#3) 已删
        ck.check("survivors exactly f1#0(id1),f2#0(id4),f2#1(null)",
                 survivors == [("f1", 0, 1), ("f2", 0, 4), ("f2", 1, None)],
                 str(survivors))

        # 重写 f1（只有 id=1 幸存）-> g1
        rw = ck.expect("rewrite f1->g1", h.post("/tables/v1/rewrite", json={
            "file_ids": ["f1"], "new_file_id": "g1"}), 200)
        ck.check("rewrite wrote 1 row", rw["result"]["rows_written"] == 1)

        # 旧位置删除必须失效且旧行号不复用
        snap = ck.expect("snapshot after rewrite", h.get("/tables/v1/snapshot"), 200)
        p3ev = next(e for e in snap["result"]["op_evaluations"] if e["delete_id"] == "p3")
        ck.check("old position op stale_row_already_removed",
                 p3ev["status"] == "stale_row_already_removed", p3ev["status"])

        # 对旧文件再下位置删除 -> 409 状态冲突
        r = h.post("/tables/v1/deletes", json={"deletes": [
            {"delete_id": "late", "kind": "position", "file_id": "f1",
             "row_number": 0}]})
        ck.expect("position on superseded -> 409", r, 409)
        ck.check("409 category state_conflict",
                 r.json()["error"]["category"] == "state_conflict")

        # 先删后插：删 id=4，再插 id=4，新行必须保留
        ck.expect("delete id=4", h.post("/tables/v1/deletes", json={
            "deletes": [{"delete_id": "del4", "kind": "equality",
                         "key": {"id": 4}}]}), 200)
        ck.expect("reload id=4 in f3", h.post("/tables/v1/load", json={
            "file_id": "f3", "source": {"kind": "inline",
                                        "rows": [{"id": 4, "name": "new4"}]}}), 201)
        q = ck.expect("query after reinsert", h.post("/tables/v1/query", json={}), 200)
        new4 = [row for row in q["result"]["rows"] if row["file_id"] == "f3"]
        ck.check("late insert kept with in_scope_insert reason",
                 len(new4) == 1 and new4[0]["keep_reason"] == "in_scope_insert"
                 and new4[0]["values"]["name"] == "new4", str(new4))

        # NULL 谓词不删 NULL 行
        ck.expect("null predicate", h.post("/tables/v1/deletes", json={
            "deletes": [{"delete_id": "dnull", "kind": "equality",
                         "key": {"id": None}}]}), 200)
        q = h.post("/tables/v1/query", json={"filters": [
            {"column": "id", "op": "is_null"}]})
        ck.check("NULL row still readable via is_null",
                 [r_["values"]["name"] for r_ in q.json()["result"]["rows"]] == ["z"])

        # 输入错误 / 资源类别（400）
        r = h.post("/tables/v1/load", json={"file_id": "bad",
                   "source": {"kind": "inline", "rows": [{"id": "x", "name": 1}]}})
        ck.expect("type mismatch -> 400", r, 400)
        ck.check("400 category input_error",
                 r.json()["error"]["category"] == "input_error")

        # 重复 delete_id 不同规格 -> 409
        r = h.post("/tables/v1/deletes", json={"deletes": [
            {"delete_id": "del2", "kind": "equality", "key": {"id": 9}}]})
        ck.expect("conflicting delete_id -> 409", r, 409)

        # run 日志可重放
        run_id = q.headers.get("x-run-id") or q.json().get("run_id")
        # 上面 q 是 200，body 带 run_id
        run_id = q.json()["run_id"]
        rec = ck.expect("replay run by id", h.get(f"/runs/{run_id}"), 200)
        ck.check("run holds state_after seq_horizon",
                 rec["state_after"]["seq_horizon"] >= 1)
        listing = ck.expect("list runs", h.get("/runs"), 200)
        ck.check("runs index non-empty", len(listing["runs"]) >= 5)

        print(f"\n结果：{ck.passed} 通过, {len(ck.failed)} 失败")
        return 1 if ck.failed else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
