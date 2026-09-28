#!/usr/bin/env python3
"""本地端到端演示脚本（合成数据，无外部依赖）。

做的事情：
1. 在临时目录启动本服务（uvicorn 后台线程，真实 HTTP）；
2. 灌入合成词典（长公共前缀/同分/全角碰撞/热词）；
3. 依次演示：长公共前缀、同分稳定排序、热词降权后上界仍正确、
   规范化碰撞、空前缀全局 top-k、剪枝上界依据、错误语义、快照/恢复；
4. 每一步带 run_id 与版本信息写入 logs/demo-<run_id>.jsonl，并在终端打印判定依据。

用法：
    python scripts/demo.py
    python scripts/demo.py --port 8765
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.normalizer import NORMALIZER_VERSION  # noqa: E402

CORPUS = [
    {"id": "mp-01", "surface": "multiprocessing", "score": 95},
    {"id": "mp-02", "surface": "multiprocessor", "score": 80},
    {"id": "mp-03", "surface": "multiprogramming", "score": 80},
    {"id": "mp-04", "surface": "multi", "score": 70},
    {"id": "mp-05", "surface": "multitask", "score": 70},
    {"id": "mp-06", "surface": "multitasking", "score": 60},
    {"id": "mp-07", "surface": "multithreading", "score": 55},
    {"id": "mp-08", "surface": "multicast", "score": 40},
    {"id": "col-1", "surface": "ＭＵＬＴＩcast", "score": 40},
    {"id": "col-2", "surface": "MULTIcast", "score": 40},
    {"id": "col-3", "surface": "MuLtIcAsT", "score": 40},
    {"id": "ot-1", "surface": "database", "score": 90},
    {"id": "ot-2", "surface": "datastore", "score": 50},
    {"id": "ot-3", "surface": "db", "score": 100},
    {"id": "zs-1", "surface": "zebra", "score": 0},
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(client: httpx.Client, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = client.get("/health")
            if r.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.15)
    raise RuntimeError(f"服务未在 {timeout}s 内就绪: {last}")


class DemoJournal:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.fh = path.open("w", encoding="utf-8")

    def step(self, title: str, payload: dict) -> None:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run_id": self.run_id,
            "normalizer_version": NORMALIZER_VERSION,
            "title": title,
            **payload,
        }
        self.fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        self.fh.flush()

    def close(self) -> None:
        self.fh.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0, help="0 表示自动选端口")
    ap.add_argument("--keep-data", action="store_true", help="使用固定 ./data 目录（默认临时目录）")
    args = ap.parse_args()

    run_id = os.environ.get("TRIE_DEMO_RUN_ID") or f"demo-{uuid.uuid4().hex[:10]}"
    port = args.port or _free_port()
    base = f"http://127.0.0.1:{port}"

    if args.keep_data:
        data_dir = Path("./data").resolve()
        log_dir = Path("./logs").resolve()
    else:
        tmp = Path(f"/tmp/trie-demo-{run_id}")
        data_dir = tmp / "data"
        log_dir = tmp / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    journal = DemoJournal(log_dir / f"demo-{run_id}.jsonl", run_id)

    settings = Settings.from_env(
        {"TRIE_DATA_DIR": str(data_dir), "TRIE_LOG_DIR": str(log_dir)}
    )
    config = uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    failures = 0

    def check(title: str, ok: bool, detail: dict) -> None:
        nonlocal failures
        verdict = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        journal.step(title, {"verdict": verdict, **detail})
        mark = "✓" if ok else "✗"
        print(f"[{mark}] {title}")
        print("    " + json.dumps(detail, ensure_ascii=False)[:400])

    import threading

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=base, timeout=10) as http:
            _wait_ready(http)
            print(f"run_id={run_id}  normalizer={NORMALIZER_VERSION}  base={base}")
            health = http.get("/health").json()
            journal.step("health", {"verdict": "INFO", "health": health})

            # 1) 灌入数据
            r = http.post("/entries/bulk", json={"items": CORPUS})
            check("批量灌入合成词典", r.status_code == 200 and r.json()["written"] == len(CORPUS),
                  {"status": r.status_code, "body": r.json() if r.status_code == 200 else r.text})

            # 2) 长公共前缀
            r = http.get("/complete", params={"prefix": "multi", "k": 5, "trace": "true"}).json()
            ids = [e["id"] for e in r["entries"]]
            check("长公共前缀 top-5",
                  ids == ["mp-01", "mp-02", "mp-03", "mp-04", "mp-05"],
                  {"ids": ids, "visited": r["trace"]["stats"]["nodes_visited"],
                   "total_nodes": r["trace"]["stats"]["total_nodes"],
                   "pruned": r["trace"]["stats"]["subtrees_pruned"]})

            # 3) 同分稳定排序（碰撞组）
            r = http.get("/complete", params={"prefix": "multicast", "k": 4}).json()
            surf = [e["surface"] for e in r["entries"]]
            check("规范化碰撞 + 同分稳定键排序",
                  surf == ["MULTIcast", "MuLtIcAsT", "multicast", "ＭＵＬＴＩcast"],
                  {"surfaces": surf, "normalized_keys": [e["normalized_key"] for e in r["entries"]]})

            # 4) 热词降权：95 -> 1，上界必须更新，不能错误剪枝
            http.post("/entries/mp-01/score", json={"score": 1})
            r = http.get("/complete", params={"prefix": "multi", "k": 3, "trace": "true"}).json()
            ids = [e["id"] for e in r["entries"]]
            prunes = r["trace"]["prunes"]
            bound_ok = all(p["upper_bound"] < p["best_k_score"] for p in prunes)
            check("热词降权后 top-3 与剪枝依据",
                  ids == ["mp-02", "mp-03", "mp-04"] and bound_ok,
                  {"ids": ids, "prunes": prunes, "rule": "subtree_max 严格小于第 k 名分数才剪"})

            # 5) 空前缀 = 全局 top-k
            r = http.get("/complete", params={"prefix": "", "k": 3}).json()
            ids = [e["id"] for e in r["entries"]]
            check("空前缀全局 top-3", ids == ["ot-3", "ot-1", "mp-02"],
                  {"ids": ids, "normalized_prefix": r["normalized_prefix"]})

            # 6) 服务端规范化（全角前缀等价）
            r = http.get("/complete", params={"prefix": "ＭＵＬＴＩ", "k": 2}).json()
            check("全角前缀服务端规范化",
                  r["normalized_prefix"] == "multi" and [e["id"] for e in r["entries"]] == ["mp-02", "mp-03"],
                  {"normalized_prefix": r["normalized_prefix"], "ids": [e["id"] for e in r["entries"]]})

            # 7) 错误语义：异常/非法输入不得返回成功
            neg = http.put("/entries/bad", json={"id": "bad", "surface": "x", "score": -3})
            missing = http.delete("/entries/ghost")
            badk = http.get("/complete", params={"k": 0})
            check("错误不透出为成功",
                  neg.status_code == 400 and missing.status_code == 404 and badk.status_code == 400
                  and neg.json()["ok"] is False and neg.json()["error"]["code"] == "E_INVALID_INPUT"
                  and missing.json()["error"]["code"] == "E_ENTRY_NOT_FOUND"
                  and badk.json()["error"]["code"] == "E_INVALID_INPUT",
                  {"neg": neg.json().get("error", {}).get("code"),
                   "missing": missing.json().get("error", {}).get("code"),
                   "bad_k": badk.json().get("error", {}).get("code")})

            # 8) 深度诊断（结构 + 与独立 oracle 对拍 + 逐条剪枝上界核算）
            r = http.post("/diagnostics/verify", params={"deep": "true"}).json()
            cc = r.get("cross_check", {})
            check("深度诊断：上界/结构 + oracle 对拍",
                  r.get("ok") is True and cc.get("ok") is True
                  and not cc.get("mismatches") and not cc.get("invalid_bounds"),
                  {"prefixes_checked": cc.get("prefixes_checked"),
                   "prune_bounds_checked": cc.get("prune_checks_total")})

            # 9) 快照与恢复
            snap = http.post("/snapshots", json={"name": "demo-base", "note": run_id})
            http.put("/entries/temp", json={"id": "temp", "surface": "multivariable", "score": 999})
            before = http.get("/complete", params={"prefix": "", "k": 1}).json()["entries"][0]["id"]
            rest = http.post("/snapshots/demo-base/restore")
            after = http.get("/complete", params={"prefix": "", "k": 1}).json()["entries"][0]["id"]
            check("持久快照创建/恢复",
                  snap.status_code == 200 and rest.status_code == 200
                  and before == "temp" and after == "ot-3",
                  {"snapshot": snap.json().get("snapshot", {}).get("name"),
                   "top_before_restore": before, "top_after_restore": after})

    finally:
        journal.close()
        server.should_exit = True
        thread.join(timeout=5)

    print(f"\n结果：{'全部通过' if failures == 0 else f'{failures} 项失败'}")
    print(f"journal: {journal.path}")
    print(f"服务日志: {log_dir}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
