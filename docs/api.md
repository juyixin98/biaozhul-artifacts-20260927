# HTTP API

基础路径 `/v1`，请求/响应均为 JSON。统一信封：

- 成功：`200 {"request_id": "...", "data": { ... }}`
- 失败：`{ "request_id": "...", "error": {"code": "...", "message": "..."}, "diag": {...} }`
- 所有响应带头 `x-request-id`；可在请求头 `x-request-id` 中自定义（字母数字与 `-_.`，≤128）。

边以不透明令牌表示：`"m<manager_id>-e<slot>"`，仅在所属管理器上可用。

## 错误码

| HTTP | code | 含义 |
|---|---|---|
| 404 | `unknown_manager` | 管理器不存在 / 已删除 |
| 422 | `parse_failed` | 文本表达式语法错误（消息带字符位置） |
| 422 | `unknown_variable` | 表达式或赋值含未声明变量 |
| 422 | `duplicate_variable` | 变量序含重复名 |
| 422 | `foreign_manager` | 边不属于路由指定的管理器 |
| 422 | `not_same_manager` | 要求同管理器的两条边来自不同管理器 |
| 422 | `malformed_edge_token` | 边令牌无法解析 |
| 422 | `mapping_rejected` | 身份映射非双射或存在未绑定变量 |
| 410 | `reclaimed_node` | 边指向的节点已被 GC 回收 |

## 端点

### `GET /healthz`
返回 `{"status":"ok","managers":N}`。

### `POST /v1/managers`
请求 `{"variable_order": ["a","b",...]}`；返回 `manager_id`、序号与变量序。

### `DELETE /v1/managers/{id}`
删除管理器；不存在返回 404。

### `POST /v1/managers/{id}/build`
```json
{ "expr": "(a & b) | (!a & c)", "root_name": "mux", "sensitive": false }
```
`expr` 与 `expr_json` 二选一；`root_name` 可选（注册后 GC 保留）。
返回 `{"edge","value","top_var","live_nodes"}`，常量函数的 `value` 为 true/false。

### `POST /v1/managers/{id}/apply`
```json
{ "op": "xor", "a": "m1-e..", "b": "m1-e..", "root_name": "r" }
```
`op`：`and | or | xor | implies | iff`。两条边必须属于同管理器。

### `POST /v1/managers/{id}/restrict`
```json
{ "edge": "m1-e..", "values": {"a": true, "c": false}, "root_name": "r" }
```
多变量按变量序升序依次限制，结果与键顺序无关。

### `POST /v1/managers/{id}/evaluate`
```json
{ "edge": "m1-e..", "assignment": {"a": true, "b": false, "c": true} }
```
未给出的变量按 false（核验用途建议给全）。返回 `{"value": bool}`。

### `POST /v1/managers/{id}/gc`
执行 mark-sweep，返回
`{"report":{"before_nodes","after_nodes","marked","swept","roots"},"roots":[...]}`。

### `GET /v1/managers/{id}/roots?include_edge=true`
列出命名根；`include_edge` 为真时附带当前边令牌。

### `POST /v1/equivalence`
```json
{
  "left_expr":  "a & (b | c)",
  "right_expr": "(a & b) | (a & c)",
  "mapping": {},
  "sensitive": false
}
```
响应：

```json
{
  "verdict": "equivalent | not_equivalent | inconclusive",
  "reason": "仅在 inconclusive 时出现",
  "result": { "verdict": "...", "identities": ["a","b","c"],
              "assignments_checked": 8,
              "witness": { "assignment": {...}, "left_value": false, "right_value": true } },
  "cross_check": { "canonical_equal": true, "truth_table_equivalent": true,
                   "assignments_checked": 8, "witness": null },
  "left_nodes": 5, "right_nodes": 5
}
```

- `result` 是**独立真值表预言机**的结论（含见证）；
- `cross_check` 是同管理器重建后的内核规范边结论与真值表结论；
- 两者矛盾时 `verdict` 为 `inconclusive`；
- 变量数超过服务端 `var_cap`（默认 20）时该路径不适用，应缩小变量集合；
- 身份映射不完整/非双射返回 422 `mapping_rejected`。
