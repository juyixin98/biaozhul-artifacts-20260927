# HTTP 接口契约

Base URL（默认）：`http://127.0.0.1:8080`。所有请求/响应均为 JSON。

## GET /health

```json
{ "status": "ok", "analyzer_version": "0.1.0" }
```

## GET /v1/version

```json
{ "analyzer_version": "0.1.0" }
```

## POST /v1/analyze

请求：

```json
{
  "source": "let x: [-10, 10];\ny := x * x;\n",
  "config": {
    "widen_delay": 1,
    "narrow_iters": 2,
    "enable_narrowing": true,
    "max_iterations": 200
  }
}
```

`config` 可选；省略字段使用服务端默认值。

成功响应（200）：

```json
{
  "request_id": "req-3fa9c1b0-...",
  "analyzer_version": "0.1.0",
  "report": {
    "analyzer_version": "0.1.0",
    "program_hash": "f4d2...",
    "config": { "...": "实际生效的配置" },
    "checks": [
      {
        "id": 0,
        "kind": "overflow | array_index | assert | unreachable_stmt",
        "span": { "line": 2, "col": 6, "offset": 17, "len": 5 },
        "verdict": "safe | maybe_violated | violated | unreachable",
        "detail": "人类可读解释（不确定结论显式写 not proven）",
        "evidence": {
          "kind": "overflow",
          "result": { "R": { "lo": { "Fin": "0" }, "hi": { "Fin": "100" } } },
          "i64_lo": -9223372036854775808,
          "i64_hi": 9223372036854775807
        }
      }
    ],
    "observations": [
      { "kind": "assign_value", "span": { "...": "Span" }, "target": "y",
        "interval": { "R": { "lo": { "Fin": "0" }, "hi": { "Fin": "100" } } } }
    ],
    "loop_invariants": [
      { "span": { "...": "while 关键字 Span" },
        "pre_state": { "AbsState": "循环前" },
        "invariant": { "AbsState": "加宽+收窄后的头部状态" } }
    ],
    "exit_state": { "vars": {}, "arrays": {}, "is_bottom": false },
    "trace": [
      { "event": "iteration", "span": {}, "iteration": 0, "head_state": {} },
      { "event": "widening", "span": {}, "iteration": 1 },
      { "event": "converged", "span": {}, "iterations": 2 },
      { "event": "narrowing_pass", "span": {}, "pass": 1 },
      { "event": "fixpoint_stabilized", "span": {}, "iterations": 2, "narrowing_passes": 2 }
    ],
    "summary": {
      "total_checks": 1, "safe": 1,
      "possible_violations": 0, "definite_violations": 0, "unreachable": 0,
      "definite_violation_ids": [], "possible_violation_ids": [], "unreachable_ids": []
    }
  }
}
```

区间 JSON 表示：`"Bottom"` 或 `{"R":{"lo": B, "hi": B}}`；
界 B 为 `"NegInf"`、`"PosInf"` 或 `{"Fin": "<十进制整数，字符串>"}`
（端点可能超出 i64，故 i128 以字符串承载；程序状态中的有限端点始终在 i64 内）。

错误响应（400），解析错误与语义错误分别标注且带位置：

```json
{
  "request_id": "req-...",
  "error": {
    "kind": "parse_error | semantic_error",
    "message": "scalar `x` used before declaration",
    "line": 1, "col": 6, "offset": 5
  }
}
```

## POST /v1/verify

请求：`{ "source": "<原文>", "report": <AnalysisReport> }`。

响应（200；注意：报告不通过验证时 HTTP 状态仍是 200，结论在 `verification.ok`）：

```json
{
  "request_id": "req-...",
  "verification": {
    "ok": false,
    "checked_invariants": 1,
    "checked_checks": 3,
    "failures": [
      { "location": "check[0]@overflow:L2:C6",
        "message": "verdict violated does not follow from its evidence (would imply safe)" }
    ]
  }
}
```

## 日志关联

服务日志为结构化文本（可用 `RUST_LOG`/`INTERVAL_ANALYZER_LOG` 调节级别），
每行带 `request_id`：

- `analysis complete`：计数总览；
- `definite failure`：每个确定失败单独一行（check_id、kind、位置、原因）；
- `uncertain conclusion`：每个可能违规单独一行，文案明确 “not proven”；
- 验证失败逐条输出 `evidence failure`（location、reason）。
