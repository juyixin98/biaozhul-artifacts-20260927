# HTTP 验证接口（rb-server）

## 约定

- 默认监听 `127.0.0.1:8080`（`RB_BIND`、`RB_DATA_DIR`、`RB_LOG` 环境变量可覆盖）。
- 请求/响应均为 JSON；每个响应都带 `x-request-id` 头。请求可用同名头指定身份
  （1–128 字符 ASCII），未指定时服务端生成 UUIDv4。
- 统一响应信封：

```json
{
  "request_id": "trace-abc",
  "format_version": "0x00010000",
  "result": { ... },
  "errors": [ { "code": "stable_code", "message": "human readable", "location": "key=7" } ],
  "notes": [ "不确定或需要关注的结论，单列" ],
  "steps": [ { "stage": "op:intersect", "detail": "..." } ]
}
```

成功时无 `errors` 字段（空数组省略）；失败时无 `result`（为 `null`）。
`notes` 与失败原因分开，绝不混在 `result` 里。

## 端点

### `GET /healthz`
返回格式版本、4096 阈值、65536 容器位数。

### `GET /v1/sets`
`result.sets`: 按名排序的集合列表。

### `POST /v1/sets?name=<n>`
请求体：
```json
{ "values": [1, 2, 65536], "expect_new": false }
```
`expect_new=true` 且名称已存在 → 409 `already_exists`。成功返回 201 与集合汇总。
名称规则：1–64 个 `[A-Za-z0-9_-]`（拒绝路径分隔、`..`、点号），否则 400 `invalid_name`。

### `GET /v1/sets/:name`
返回 `SetSummary`：`cardinality`(u64)、`containers`、
`container_kinds: {array, bitmap}`、`min`、`max`、`persisted`。

### `PUT /v1/sets/:name`
用请求体 `{"values":[...]}` 整体替换（upsert），原子落盘。

### `DELETE /v1/sets/:name`
删除；不存在 → 404。

### `POST /v1/sets/:name/values`
追加成员（集合不存在则创建）；重复值折叠，响应 step 给出提交数与新增数。

### `GET /v1/sets/:name/values?limit=100`
按升序返回至多 `limit`（≤10000）个值；被截断时 `truncated=true` 且 notes 说明
总基数——集合运算始终精确，只有显式取值会分页。

### `GET /v1/sets/:name/contains/:value`
`{"value": v, "contains": bool}`。

### `GET /v1/sets/:name/rank/:x`
`rank` = 严格小于 `x` 的元素个数（u64）。

### `GET /v1/sets/:name/select/:i`
第 `i`（0 基）小元素；越界时 `value=null` 且 notes 注明 `i >= cardinality`。

### `POST /v1/sets/:name/union|intersect|difference`
请求体 `{"with":"other"}`。返回结果集合的汇总（`persisted=false`——运算不写盘），
`steps` 展示两侧容器构成、所用算法路径与结果容器构成，`notes` 注明未展开全量整数集合。

## 错误码

| HTTP | code | 触发条件 |
|---|---|---|
| 400 | `invalid_json` | 请求体不是合法 JSON 或字段类型错误 |
| 400 | `invalid_name` | 名称不合法（含目录逃逸尝试） |
| 404 | `set_not_found` | 集合不存在 |
| 409 | `already_exists` | `expect_new` 与现存名称冲突 |
| 422 | `corrupt_truncated` / `corrupt_bad_magic` / `corrupt_unsupported_version` / `corrupt_header_checksum` / `corrupt_body_checksum` / `corrupt_unknown_container_tag` / `corrupt_keys_not_sorted` / `corrupt_bad_offset` / `corrupt_bad_payload_length` / `corrupt_array_not_sorted` / `corrupt_threshold_violation` / `corrupt_cardinality_mismatch` / `corrupt_trailing_bits` | 磁盘文件未通过严格校验（见 FORMAT.md §4） |
| 500 | `io_error` | 文件系统故障（非数据语义错误） |

所有错误响应同样带 `request_id`（响应头与响应体一致），日志以 JSON 行输出且
每条记录含该 id，便于把一次请求的处理步骤、处理位置（高 16 位键等）与失败关联起来。
