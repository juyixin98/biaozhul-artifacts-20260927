"""可重放的请求样例：对本机运行中的服务依次发起创建/转换/编辑/诊断请求。

用法：
    .venv/bin/python examples/requests_demo.py            # 默认 127.0.0.1:8000
    BASE=http://127.0.0.1:8931 .venv/bin/python examples/requests_demo.py
所有输入均为本地合成文本，base64 载荷在脚本内现算。
"""
from __future__ import annotations

import base64
import json
import os

import httpx

BASE = os.environ.get("BASE", "http://127.0.0.1:8000")


def b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def show(title: str, resp: httpx.Response) -> None:
    print(f"\n### {title}  [{resp.status_code}]")
    print(json.dumps(resp.json(), ensure_ascii=False, indent=2))


def main() -> None:
    # 合成文本：字母 + 预组合字符 + 旗帜(2 RI) + ZWJ 家庭表情 + CRLF
    text = "aé🇺🇳👨‍👩‍👧\r\nb"
    with httpx.Client(base_url=BASE, timeout=10) as c:
        show("health（固定 Unicode 版本）", c.get("/health"))

        r = c.post(
            "/documents",
            json={"doc_id": "demo", "content_base64": b64(text)},
            headers={"X-Run-ID": "example-run"},
        )
        show("POST /documents 创建（非法 UTF-8 会被拒）", r)
        r.raise_for_status()

        show(
            "字节 15 → 簇序号（ZWJ 家庭簇起点）",
            c.get("/documents/demo/convert",
                  params={"position": 15, "from_space": "byte", "to_space": "cluster"}),
        )
        show(
            "簇 2 → 字节（旗帜簇起点）",
            c.get("/documents/demo/convert",
                  params={"position": 2, "from_space": "cluster", "to_space": "byte"}),
        )
        bad = c.get("/documents/demo/convert",
                    params={"position": 16, "from_space": "byte", "to_space": "codepoint"})
        show("字节 16（表情多字节内部）→ NOT_A_BOUNDARY", bad)

        edit = c.post(
            "/documents/demo/edits",
            json={
                "expected_version": 0,
                "start": 2, "end": 3, "space": "cluster",
                "replacement_base64": b64("👦🏽"),  # 用簇边界整簇替换旗帜
            },
        )
        show("POST /documents/demo/edits 增量编辑（服务端强制对照全量重建）", edit)

        show("版本链", c.get("/documents/demo/versions"))
        show("逐簇诊断（GCB 属性可见）", c.get("/documents/demo/clusters", params={"limit": 4}))
        show("按 run_id 回放操作日志", c.get("/diagnostics/runs/example-run"))


if __name__ == "__main__":
    main()
