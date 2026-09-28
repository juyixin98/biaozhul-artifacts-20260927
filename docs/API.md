# HTTP API

`Content-Type: application/json`。所有时间戳为 Unix 毫秒。身份：客户端可传
`X-Request-Id`（1–64 个 ASCII 可见安全字符，否则服务端生成）；响应回
`X-Request-Id` 与 `X-Run-Id`。

## POST /v1/tables —— 建表（冻结坐标）

请求：
```json
{"xs": [3, 1, 3], "ys": [10, 20, 20]}
```
`201`：
```json
{"ok": true, "table_id": 1, "version": 0,
 "nx": 2, "ny": 2, "duplicate_x": 1, "duplicate_y": 1, "created_at_ms": 1790000000000}
```
失败：`400 EMPTY_COORDINATES`、`400 BAD_JSON`。

## POST /v1/tables/:id/batches —— 原子批更新

请求（`base_version` 可省略，表示基于最新）：
```json
{"base_version": 0,
 "updates": [{"x": 1, "y": 10, "delta": 12}, {"x": 3, "y": 20, "delta": -4}]}
```
`200`：
```json
{"ok": true, "table_id": 1, "version": 1, "base_version": 0,
 "update_count": 2, "created_at_ms": 1790000001000}
```
失败：`422 COORDINATE_NOT_REGISTERED` / `422 POINT_OVERFLOW` /
`400 EMPTY_BATCH` / `409 STALE_BASE_VERSION` /
`404 VERSION_NOT_FOUND` / `404 TABLE_NOT_FOUND`。

## POST /v1/tables/:id/query —— 闭区间矩形和

请求（`version` 可省略查最新；边界为闭区间）：
```json
{"version": 1, "x_lo": 1, "x_hi": 3, "y_lo": 10, "y_hi": 20}
```
`200`：
```json
{"ok": true, "table_id": 1, "version": 1,
 "rect": {"x_lo": 1, "x_hi": 3, "y_lo": 10, "y_hi": 20},
 "sum": 8, "empty": false,
 "selected_coordinates": {"x": 2, "y": 2}}
```
空矩形（区间内无注册坐标）：`200 {"sum": 0, "empty": true, ...}`。
失败：`400 INVERTED_RECT`、`404 VERSION_NOT_FOUND`、`422 SUM_OVERFLOW`。

极端坐标可以直接用：`x_lo = -9223372036854775808`、`x_hi = 9223372036854775807`
表示全域；实现不做 `lo-1` 算术，不会溢出。

## GET /v1/tables/:id —— 表元信息

```json
{"ok": true, "table_id": 1, "latest_version": 1, "xs": [1, 3], "ys": [10, 20]}
```

## GET /v1/tables/:id/versions —— 版本链

```json
{"ok": true, "table_id": 1, "latest_version": 1,
 "versions": [
   {"version": 0, "base_version": 0, "update_count": 0, "created_at_ms": 1790000000000},
   {"version": 1, "base_version": 0, "update_count": 2, "created_at_ms": 1790000001000}
 ]}
```

## GET /health

```json
{"ok": true, "run_id": "run-…-pid1234", "tables": [1]}
```

## 错误信封（任何失败，状态码都不是 200）

```json
{"ok": false,
 "error": {"code": "COORDINATE_NOT_REGISTERED",
           "message": "coordinate (9, 10) is not registered; …",
           "request_id": "case-…-0007",
           "run_id": "run-…-pid1234"}}
```

完整错误码与状态映射见 [`docs/SEMANTICS.md`](SEMANTICS.md)。

## curl 串讲

```bash
curl -s -XPOST localhost:8080/v1/tables -d '{"xs":[1,2,3],"ys":[10,20]}'
curl -s -XPOST localhost:8080/v1/tables/1/batches \
  -d '{"updates":[{"x":1,"y":10,"delta":5},{"x":3,"y":20,"delta":-2}]}'
curl -s -XPOST localhost:8080/v1/tables/1/query \
  -d '{"x_lo":1,"x_hi":3,"y_lo":10,"y_hi":20}'
```
