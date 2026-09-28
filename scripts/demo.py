#!/usr/bin/env python3
"""本地端到端演示（不启动 HTTP，直接走服务层；全部使用合成数据）。

    python scripts/demo.py

演示要点：
  1. 上传多字节源文本；
  2. 多条规则（捕获引用 / 零宽插入 / 重叠优先级）构建计划并打印字节范围；
  3. 流式应用，打印结果与新版本；
  4. 改写源后，旧计划应用被 STATE_SOURCE_VERSION_MISMATCH 拒绝。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.diagnostics import Diag
from app.errors import SourceVersionMismatchError
from app.schemas import PlanRequest, RuleIn
from app.service import Service
from app.storage import Repository


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="nrp-demo-")) / "demo.db"
    repo = Repository(tmp)
    svc = Service(repo)

    text = "alpha@example.com 与 beta@test.org；编号A-1，零宽演示。"
    src = svc.upload_source(text)
    print(f"[1] 上传源 {src['source_id']} v{src['version']} "
          f"({src['spec']['char_len']} 码点 / {src['spec']['byte_len']} 字节)")

    rules = [
        RuleIn(rule_id="mail", pattern=r"(\w+)@(\w+)\.(\w+)",
               template=r"[\1@\2.\3]", priority=10),
        RuleIn(rule_id="code", pattern=r"编号([A-Z])-(\d)",
               template=r"CODE(\1/\2)", priority=20),
    ]
    diag = Diag(repo)
    summary, run_id = svc.create_plan(
        PlanRequest(source_id=src["source_id"], rules=rules), diag
    )
    # 另建一个计划，稍后用于演示“源已变更、计划未应用”的版本守卫
    pending, _ = svc.create_plan(
        PlanRequest(source_id=src["source_id"], rules=rules), Diag(repo)
    )
    print(f"[2] 计划 {summary.plan_id}: 入选 {summary.replacement_count} 条 "
          f"(零宽 {summary.zero_width_count})，run_id={run_id}")
    detail, _, _, _ = svc.get_plan_detail(summary.plan_id)
    for r in detail.replacements:
        print(f"    #{r.index} {r.rule_id} 码点[{r.char_start},{r.char_end}) "
              f"字节[{r.byte_start},{r.byte_end}) {r.matched!r} -> {r.replacement!r}")

    gen, meta, _ = svc.apply_plan_stream(summary.plan_id, Diag(repo), chunk_chars=16)
    chunks = list(gen())
    out_text = "".join(chunks)
    print(f"[3] 流式应用：{len(chunks)} 个分片，新版本 v{meta['source_version']} -> 已提交")
    print(f"    结果: {out_text}")

    svc.replace_source(src["source_id"], text + "  # 被外部修改")
    print("[4] 源已被改写为新版本；用尚未应用的旧计划应用，应被拒绝：")
    try:
        gen2, _, _ = svc.apply_plan_stream(pending.plan_id, Diag(repo))
        list(gen2())
    except SourceVersionMismatchError as exc:
        print(f"    拒绝成功: {exc.code} ({exc.category}) HTTP {exc.http_status}")
        print(f"    详情: {exc.details}")
    else:
        raise SystemExit("不应成功应用过期计划！")

    print(f"\n诊断日志（可重放）: scripts/replay.py {run_id}")
    repo.close()


if __name__ == "__main__":
    main()
