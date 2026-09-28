# HTTP 接口

Base URL：`http://127.0.0.1:8080`（可用 `SQLGUARD_*` 环境变量改地址）。

## `GET /health`

就绪状态与审查依据（策略 id、schema 摘要、已知表）。

## `POST /api/v1/audit/reviews`

请求体：

```json
{
  "sql": "SELECT id FROM orders WHERE status = ANY(:s) ORDER BY ${c} ${d}",
  "parameters": {"s": ["paid", "shipped"]},
  "identifiers": {"c": "created_at", "d": "DESC"}
}
```

- `parameters`：值绑定。命名/编号用对象，位置占位 `?` 用数组。
- `identifiers`：`${slot}` 标识符绑定。
- 请求头 `X-Request-Id` 可透传追踪 id；否则服务端生成。

响应（节选）：

```json
{
  "verdict": "accept",
  "request_id": "rev_0da1...",
  "sql_digest": "f190be879937cd59",
  "findings": [],
  "bound_parameters": [
    {"ref": "s", "style": ":", "position": "array", "kind": "array",
     "length": 2, "elements": [
        {"kind": "string", "length": 4, "fingerprint": "..."},
        {"kind": "string", "length": 7, "fingerprint": "..."}
     ]}
  ],
  "identifier_bindings": [
    {"slot": "c", "role": "column", "bound": "created_at",
     "table": "orders", "matched_whitelist": true},
    {"slot": "d", "role": "keyword", "bound": "DESC",
     "matched_whitelist": true}
  ],
  "inert_occurrences": [],
  "statements": ["select"],
  "basis": {
    "policy_id": "shop-readonly-v1",
    "dialect": "sqlite",
    "schema_digest": "9ad7dbc3c666f50a",
    "tables": ["audit_events", "orders", "products", "users"]
  },
  "limitations": [],
  "diagnostics": {
    "request_id": "rev_0da1...",
    "sql_digest": "f190be879937cd59",
    "parameter_refs": [["s", ":"]],
    "slot_refs": ["c", "d"],
    "decision": "accept",
    "reason_codes": []
  },
  "stored": {"request_id": "rev_0da1...", "stored": true,
             "record_index": "ab12..."}
}
```

拒绝示例：

```json
{
  "verdict": "reject",
  "findings": [{
    "code": "VALUE_USED_AS_IDENTIFIER",
    "severity": "error",
    "message": "value parameter '?' appears in a table-name position; ...",
    "span": {"start": 15, "end": 16, "line": 1},
    "detail": {}
  }],
  "diagnostics": {"decision": "reject", "reason_codes": ["VALUE_USED_AS_IDENTIFIER"]}
}
```

## `GET /api/v1/audit/reviews/{request_id}`

取回一条加密审计记录（服务端解密后返回）；不存在返回 404。

## `GET /api/v1/audit/reviews?limit=20`

最近记录的**仅结论**元数据（不解密正文）：`record_index` / `verdict` /
`created_at`。

## 状态码约定

- `200`：审查完成（结论可能是 accept / reject / unanalyzable）。
- `422`：请求体不符合模型（如空白 SQL）。
- `404`：审计记录不存在。
- `503`：夹具不可用，服务未就绪。
- `500`：内部错误；响应同样带 request id 且内容脱敏。

## 配置（环境变量）

| 变量 | 默认 |
|---|---|
| `SQLGUARD_POLICY` | `configs/policy.json` |
| `SQLGUARD_FIXTURE` | `fixtures/shop.db` |
| `SQLGUARD_AUDIT_DB` | `runtime/audit.db` |
| `SQLGUARD_AUDIT_KEY` | `runtime/audit.key`（不存在时生成，0600） |
| `SQLGUARD_HOST` / `SQLGUARD_PORT` | `127.0.0.1` / `8080` |
