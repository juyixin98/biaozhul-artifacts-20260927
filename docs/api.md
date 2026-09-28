# HTTP 回放接口

仅监听 `127.0.0.1`，标准库 `net/http`。所有时间为 RFC3339（建议 UTC）。

## `GET /healthz`

```json
{"status":"ok"}
```

## `POST /runs/{runID}/packets`

评估单个报文。run 不存在时自动创建（时钟以首个时间戳初始化）。

请求体（五元组字段平铺）：

```json
{
  "seq": 0,
  "ts": "2026-01-01T00:00:00Z",
  "src_ip": "10.0.0.10", "src_port": 5001,
  "dst_ip": "198.51.100.1", "dst_port": 53,
  "protocol": "TCP|UDP",
  "direction": "outbound|inbound",
  "flags": "SYN|SYN+ACK|ACK|FIN+ACK|RST",
  "fragment": {"offset": 0, "more_fragments": false}
}
```

- 出站：`src_ip` 必须落在 `private_cidrs` 内。
- 入站：`dst_ip` 必须等于配置的 `public_ip`，`dst_port` 为外部端口。
- `fragment` 仅在确为分片（offset>0 或 MF=true）时给出；重组后的正常报文省略它。

成功判决（HTTP 200，模型拒绝也返回 200，用 `accepted` 区分）：

```json
{
  "run_id": "demo", "seq": 0, "accepted": true,
  "observed_at": "2026-01-01T00:00:00Z",
  "effective_at": "2026-01-01T00:00:00Z",
  "clock_rewind": false, "swept_expired": 0, "active_mappings": 1,
  "mapped_port": 40000, "state": "open",
  "translated": {"src_ip":"203.0.113.1","src_port":40000,
                 "dst_ip":"198.51.100.1","dst_port":53,"protocol":"UDP"}
}
```

拒绝判决（HTTP 200）：

```json
{"run_id":"demo","accepted":false,
 "category":"state_conflict","code":"ENDPOINT_FILTERED",
 "reason":"endpoint-dependent filter: remote ... != mapped peer ...",
 "observed_at":"...","effective_at":"...","active_mappings":1}
```

错误归类：模型判决（`invalid_input`/`state_conflict`/`resource_exhausted`）返回 200；
JSON 语法错误等边界输入错误返回 **400**（`BAD_JSON`）；存储/内部故障返回 **500**
（`compute_failure`/`STORE_ERROR`）。

## `POST /runs/{runID}/packets/batch`

请求体为报文对象数组，**按数组顺序**串行评估，返回判决数组。适合脚本化灌包。
（真正的并发首包竞赛由 `replay` 夹具的同 `group` 触发，见 architecture 文档。）

## `GET /runs/{runID}/events?limit=N`

返回判决日志（按写入顺序）。每行包含：`id, seq, observed_at, effective_at,
clock_rewind, accepted, category, code, reason, mapped_port, state, packet, detail`。
`detail` 内含 `active_count / swept / clock_rewind / translated / expires_at` 等中间状态。

## `GET /runs/{runID}/mappings?active=true`

返回映射行：`id, protocol, src_ip, src_port, dst_ip, dst_port, mapped_port, state,
created_at, last_used_at, expires_at`。`active=true` 只返回非 closed 行（不按墙钟剔除，
因为回放使用合成时间；调用方用 `expires_at` 与 run 的生效时钟比较）。

## 重放问题需要保留的信息

- run id（URL 中的 `{runID}`，或 `replay -run-id`）；
- 事件序号 `seq` / 事件行 `id`；
- `observed_at` 与 `effective_at`（区分时间回拨）、`clock_rewind`；
- `swept_expired`、`active_mappings`、映射 `state` 与 `expires_at`；
- `category` + `code` + `reason`。
