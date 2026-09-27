#!/usr/bin/env python3
"""服务调用示例：成功查询、等价书写版本号对照、三类失败响应。

用法：先启动服务（uvicorn service.main:app --port 8000），再运行本脚本。
"""

import json
import os
import sys

import httpx

BASE = os.environ.get("SEARCHDSL_URL", "http://127.0.0.1:8000")


def show(title: str, resp: httpx.Response) -> None:
    print(f"\n=== {title} [{resp.status_code}] ===")
    try:
        print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print(resp.text)


def main() -> int:
    queries = [
        ("成功：字段 + 括号 + 隐式 AND", "apple AND (pie OR salad)"),
        ("成功：短语（引号内 OR 不是运算符）", 'title:"red apple"'),
        ("成功：空查询 = 全部文档", ""),
        ("等价书写 1", "apple AND pie"),
        ("等价书写 2（应返回相同 version）", "pie AND apple"),
        ("400 语法错误（位置 7）", "a AND OR b"),
        ("422 未知字段", "foo:bar"),
        ("422 类型错误", "year:abc"),
        ("413 预算超限", "apple AND (apple AND (apple AND (apple AND (apple AND (apple AND (apple AND (apple AND apple)))))))"),
    ]
    with httpx.Client(base_url=BASE, timeout=10) as client:
        versions = []
        for title, q in queries:
            resp = client.post("/query", json={"query": q})
            show(title, resp)
            if resp.status_code == 200:
                versions.append((q, resp.json()["version"]))

        print("\n=== 版本号对照 ===")
        for q, v in versions:
            print(f"{v}  <= {q!r}")

        print("\n=== 版本存储查询 ===")
        any_version = next(v for q, v in versions if q == "apple AND pie")
        show(f"GET /queries/{any_version}", client.get(f"/queries/{any_version}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
