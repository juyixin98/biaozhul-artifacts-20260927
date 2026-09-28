"""最小服务调用客户端（仅用标准库 urllib，无第三方依赖）。

用法：
    python examples/client_example.py                 # 默认 http://127.0.0.1:8332
    BASE=http://host:port python examples/client_example.py

会依次：健康检查 → verify 合法交易 → submit 合法交易 → 重复提交（STATE 冲突）
→ 提交重复签名用例（COMPUTE 失败），并打印结构化失败分类与 run_id。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("BASE", "http://127.0.0.1:8332")
ROOT = Path(__file__).resolve().parent.parent


def post(path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=10) as resp:
        return json.loads(resp.read())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n== {title} ==")
    print(f"HTTP {status}")
    keep = {k: body.get(k) for k in
            ("accepted", "kind", "code", "detail", "txid", "height", "run_id")
            if k in body}
    print(json.dumps(keep, ensure_ascii=False, indent=2))


def main() -> int:
    cases = json.loads((ROOT / "fixtures" / "cases.json").read_text("utf-8"))["cases"]

    print("health:", json.dumps(get("/health"), ensure_ascii=False))

    st, body = post("/transactions/verify", cases["00"]["tx"])
    show("verify 合法 P2PK（不应改状态）", st, body)
    assert st == 200 and body["accepted"] is True

    st, body = post("/transactions/submit", cases["20"]["tx"])
    show("submit 合法交易（201）", st, body)
    assert st == 201 and body["accepted"] is True

    st, body = post("/transactions/submit", cases["20"]["tx"])
    show("重复提交（预期 STATE/TX_ALREADY_ACCEPTED）", st, body)
    assert st == 422 and body["code"] == "TX_ALREADY_ACCEPTED"

    st, body = post("/transactions/submit", cases["05"]["tx"])
    show("重复签名（预期 COMPUTE/SIG_DUPLICATED，无转账）", st, body)
    assert st == 422 and body["code"] == "SIG_DUPLICATED"

    st, body = post("/transactions/submit", cases["07"]["tx"])
    show("错误交易域（预期 COMPUTE/SIG_INVALID）", st, body)
    assert st == 422 and body["code"] == "SIG_INVALID"

    state = get("/state")
    print("\nstate replay:",
          json.dumps({k: state[k] for k in ("height", "state_root")},
                     ensure_ascii=False))
    print("replay:", json.dumps(state["replay"], ensure_ascii=False)[:300], "...")
    print("\n全部调用符合预期。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
