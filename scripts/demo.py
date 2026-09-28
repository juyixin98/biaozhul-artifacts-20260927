#!/usr/bin/env python3
"""本地端到端演示脚本（合成夹具，无外部依赖）。

直接运行::

    python scripts/demo.py

它会在临时目录创建数据库并通过 HTTP（FastAPI TestClient，无需先起服务）
依次演示：

1. 长公共前缀词族的压缩索引与补全；
2. 同分词条按稳定规范键排序（含规范化碰撞）；
3. 热词降权后可靠上界收缩、结果与剪枝依据；
4. 规范化碰撞（全角/连字/大小写）同一键共存、原文保留；
5. 空前缀全局 top-k；
以及持久快照与历史查询、恢复。

每个步骤打印版本号、输入、计算步骤/剪枝判定，失败时以非零码退出
（不会把异常状态打印成成功）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.api import create_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.engine import Engine  # noqa: E402


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def must(resp, expected=(200, 201), label: str = "") -> dict:
    if resp.status_code not in expected:
        print(f"[FATAL] {label} 期望 {expected}，实际 {resp.status_code}: {resp.text}")
        sys.exit(2)
    return resp.json()


def show_complete(client, prefix: str, k: int, diag: bool = False) -> dict:
    params = {"prefix": prefix, "k": k, "diagnostics": str(diag).lower()}
    body = must(client.get("/api/v1/complete", params=params), label=f"complete {prefix!r}")
    print(f"查询 prefix={prefix!r} k={k} 规范化={body['prefix_norm']!r} "
          f"version=v{body['version']} 命中 {body['count']} 条：")
    for r in body["results"]:
        print(f"  score={r['score']:>6}  id={r['id']:<10} term_norm={r['term_norm']!r}"
              f"  原文={r['term']!r}")
    if diag and "diagnostics" in body:
        d = body["diagnostics"]
        print(f"  [计算步骤] 入堆={d['pushed']} 展开节点={d['expanded']} "
              f"扫描终止词条={d['terminals_seen']} 整枝剪枝={d['pruned_children']} "
              f"堆弹出={d['heap_pops']}")
        for dec in d["decisions"]:
            if dec["decision"] == "prune":
                print(f"    - 剪枝 node={dec['node_id']} 边={dec['edge_label']!r} "
                      f"子树上界={dec['subtree_best_score']} "
                      f"阈值={dec['threshold_score']}")
                print(f"      依据: {dec['reason']}")
    return body


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="ctrie-demo-")
    db_path = str(Path(tmpdir) / "demo.db")
    settings = Settings(db_path=db_path, topk_max=100)
    engine = Engine(db_path, topk_max=100)
    client = TestClient(create_app(settings=settings, engine=engine))
    client.headers.update({"X-Request-ID": "demo-run"})

    print(f"演示数据库: {db_path}")
    print(f"健康检查: {must(client.get('/health'))}")

    # ---------------------------------------------------------- 1. 长公共前缀
    banner("1. 长公共前缀词族（验证压缩 + 前缀定位）")
    long_words = [
        ("w1", "internationalization", 10),
        ("w2", "internationalise", 9),
        ("w3", "internationally", 8),
        ("w4", "internet", 20),
        ("w5", "internal", 15),
        ("w6", "interstellar", 5),
        ("w7", "interval", 7),
    ]
    must(client.post("/api/v1/entries:bulkUpsert", json={
        "entries": [{"id": i, "term": t, "score": s} for i, t, s in long_words],
        "note": "long common prefix family",
    }), label="bulk long prefix")
    show_complete(client, "intern", 4)
    show_complete(client, "internation", 3)
    status = must(client.get("/api/v1/status"))
    print(f"结构: 存储词条={status['stored_entries']} Trie节点={status['node_count']} "
          f"最大深度={status['max_depth']} 健康={status['healthy']}")

    # ---------------------------------------------------------- 2. 同分稳定序
    banner("2. 同分按稳定规范键排序（(term_norm, display, id)）")
    ties = [
        ("t1", "tie_banana", 5), ("t2", "tie_apple", 5),
        ("t3", "tie_cherry", 5), ("t4", "tie_avocado", 5),
    ]
    must(client.post("/api/v1/entries:bulkUpsert", json={
        "entries": [{"id": i, "term": t, "score": s} for i, t, s in ties],
    }), label="bulk ties")
    body = show_complete(client, "tie", 4)
    ordered = [r["term"] for r in body["results"]]
    assert ordered == ["tie_apple", "tie_avocado", "tie_banana", "tie_cherry"], ordered
    print("  -> 次序断言通过: tie_apple < tie_avocado < tie_banana < tie_cherry")

    # ---------------------------------------------------------- 3. 热词降权
    banner("3. 热词降权：上界必须收缩，被整枝剪枝")
    hot = [
        ("h1", "aaa_hot1", 100), ("h2", "aaa_hot2", 99),
        ("c1", "bbb_cold1", 50), ("c2", "bbb_cold2", 49),
        ("c3", "bbb_cold3", 48),
    ]
    must(client.post("/api/v1/entries:bulkUpsert", json={
        "entries": [{"id": i, "term": t, "score": s} for i, t, s in hot],
    }), label="bulk hot")
    print("降权前 top2:", [r["id"] for r in show_complete(client, "", 2)["results"]])
    must(client.post("/api/v1/entries:bulkUpsert", json={
        "entries": [
            {"id": "h1", "term": "aaa_hot1", "score": 1},
            {"id": "h2", "term": "aaa_hot2", "score": 0},
        ],
        "note": "demote hot words",
    }), label="demote")
    print("降权后（含剪枝依据）:")
    show_complete(client, "", 2, diag=True)
    inv = must(client.get("/api/v1/diagnostics/invariants"))
    assert inv["healthy"], inv
    print("  -> 不变量诊断通过（上界已沿链重算）")

    # ---------------------------------------------------------- 4. 规范化碰撞
    banner("4. 规范化碰撞：不同原文共享索引键，原文保留")
    collisions = [
        ("u1", "ＣＡＦＥ", 7),   # 全角
        ("u2", "cafe", 7),       # 小写
        ("u3", "CAFE", 7),       # 大写
        ("u4", "ﬁle", 4),        # 连字 fi -> NFKC -> "file"
        ("u5", "FILE", 4),
    ]
    must(client.post("/api/v1/entries:bulkUpsert", json={
        "entries": [{"id": i, "term": t, "score": s} for i, t, s in collisions],
    }), label="bulk collisions")
    show_complete(client, "CAFE", 10)  # 大写查询同样命中
    show_complete(client, "ﬁl", 10)    # 连字前缀与 ASCII 等价命中

    # ---------------------------------------------------------- 5. 空前缀 + 快照
    banner("5. 空前缀全局 top-k + 持久快照 / 历史查询 / 恢复")
    snap = must(client.post("/api/v1/snapshots", json={"note": "freeze-before-edits"}))
    print(f"已创建快照 v{snap['version']}（{snap['entry_count']} 条）")
    before = show_complete(client, "", 3)
    # 在快照之后做大量改动
    must(client.post("/api/v1/entries:bulkUpsert", json={
        "entries": [{"id": "zzz", "term": "zzz_new_champion", "score": 999}],
    }), label="post-snapshot edit")
    must(client.post("/api/v1/entries:delete", json={"id": "w4"}), label="delete w4")
    print("快照之后的当前 top3:")
    show_complete(client, "", 3)
    print(f"快照 v{snap['version']} 时刻的历史 top3（应包含旧数据，不含 zzz）:")
    hist = must(client.get("/api/v1/complete", params={
        "prefix": "", "k": 3, "version": snap["version"],
    }), label="historical")
    for r in hist["results"]:
        print(f"  score={r['score']:>6} id={r['id']:<10} 原文={r['term']!r}")
    assert all(r["id"] != "zzz" for r in hist["results"]), "历史查询泄漏了快照后数据"

    restored = must(client.post("/api/v1/snapshots:restore", json={
        "version": snap["version"], "note": "demo restore",
    }), label="restore")
    print(f"已从 v{snap['version']} 恢复 -> 新版本 v{restored['version']}，"
          f"词条数={restored['entry_count']}")
    after = show_complete(client, "", 3)
    assert before["results"] == after["results"], "恢复后结果与快照不一致"
    print("  -> 恢复后 top3 与快照时刻一致")

    versions = must(client.get("/api/v1/versions?limit=5"))
    print("最近版本链:")
    for v in versions:
        print(f"  v{v['version_id']:<3} parent={v['parent_id']} kind={v['kind']:<9}"
              f" entries={v['entry_count']:<4} note={v['note']!r}")

    banner("演示全部通过 ✅")
    print(f"（数据库保留在 {db_path}，可用 "
          f"CTRIE_DB_PATH={db_path} python -m app.main 重新打开）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
