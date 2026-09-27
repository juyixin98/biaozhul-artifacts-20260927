# HTTP API 参考

所有响应都是 JSON；每个响应带 `X-Request-Id` 头，与服务器日志中的
`req=...` 对应。错误响应形如：

```json
{"error": {"category": "invalid_config", "message": "field interfaces: ..."}}
```

## POST /v1/replay

执行一个场景，持久化到 SQLite，返回完整结果（含 `run_id`）。

- `200`：回放完成（即使存在被拒绝的事件——见结果中的 `rejections`）。
- `400 bad_request`：请求体不是合法 JSON。
- `422 invalid_config | invalid_scenario`：配置或场景结构不合法。

示例：

```sh
curl -s -X POST http://127.0.0.1:8080/v1/replay -d '{
  "name": "demo",
  "config": {"interfaces": ["eth0"], "query_interval_sec": 100,
             "query_response_interval_sec": 10, "robustness_variable": 2,
             "last_member_query_interval_sec": 5, "last_member_query_count": 3},
  "events": [
    {"time_ms": 0, "type": "report", "iface": "eth0", "group": "239.1.1.1", "member": "10.0.0.1"},
    {"time_ms": 50000, "type": "leave", "iface": "eth0", "group": "239.1.1.1", "member": "10.0.0.1"}
  ],
  "run_until": 100000
}'
```

结果中 `intervals["eth0/239.1.1.1"]` 为 `[{"start_ms":0,"end_ms":65000}]`
（最后成员离开后 50000+15000 到期）。

## GET /v1/runs

列出全部已持久化运行（新→旧）：`{"runs": [{"id":1,"name":"...","created_at":"..."}]}`。

## GET /v1/runs/{id}

取回某次运行的完整结果。`404 run_not_found`：ID 不存在。

## GET /healthz

`{"ok": true}`。

## SQLite 直接检查

持久化的运行也可以用任意 SQLite 客户端检查：

```sh
sqlite3 igmpq.db 'SELECT seq, time_ms, type, reason FROM transitions WHERE run_id=1;'
sqlite3 igmpq.db 'SELECT iface, grp, start_ms, end_ms FROM intervals WHERE run_id=1;'
```

表：`runs`（含完整结果 JSON）、`events`、`transitions`、`intervals`、
`rejections`。
