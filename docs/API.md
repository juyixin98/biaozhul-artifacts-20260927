# HTTP API

所有接口默认绑定 `127.0.0.1:8080`（配置 `http.listen`）。请求/响应均为 JSON（流回放接口
除外）。错误体形如：

```json
{"error": "human message", "request_id": "req-…", "category": "validation|duplicate_request|pcap_parse_failed|not_found|internal"}
```

失败类别是稳定字符串，调用方应据此分支而不是只看状态码。

## 入库

### POST /api/v1/ingest — JSON 报文入库

报文按数组顺序视为到达顺序（服务端不重排）。`seq`/`ack` 为原始 32 位无符号整数（JSON number）。
`payload` 为 base64 字符串。`record_id` 可选，缺省由服务端分配 `rec-00001`…。

```json
{
  "request_id": "req-demo-001",
  "packets": [
    {"src_ip":"10.0.0.1","src_port":40001,"dst_ip":"10.0.0.2","dst_port":80,
     "flags":["SYN"],"seq":4294967286,"record_id":"c-001"},
    {"src_ip":"10.0.0.2","src_port":80,"dst_ip":"10.0.0.1","dst_port":40001,
     "flags":["SYN","ACK"],"seq":7000,"ack":4294967287,"record_id":"s-001"},
    {"src_ip":"10.0.0.1","src_port":40001,"dst_ip":"10.0.0.2","dst_port":80,
     "flags":["ACK","PSH"],"seq":4294967287,"payload":"SEVMTG8t","record_id":"c-002"}
  ]
}
```

响应 `201 Created`：

```json
{
  "request_id": "req-demo-001",
  "source": "json",
  "policy": "quarantine",
  "packet_count": 3,
  "created_at": "2026-09-27T…Z",
  "conflict_count": 0,
  "events_by_level": {"info": 3},
  "generations": [
    {"flow":"10.0.0.1:40001<->10.0.0.2:80","generation":0,"closed":false,"reset":false,
     "a_to_b_contiguous_bytes":5,"b_to_a_contiguous_bytes":0,"a_to_b_gaps":0,"b_to_a_gaps":0}
  ]
}
```

### POST /api/v1/ingest/pcap?request_id=…&source=… — pcap 入库

请求体为经典 libpcap 二进制（Ethernet/raw IP/Linux cooked）。解析失败返回
`400 pcap_parse_failed`。pcap 不携带 record id，服务端按到达顺序分配。

## 查询

| 方法与路径 | 说明 |
|---|---|
| `GET /healthz` | 存活检查 |
| `GET /api/v1/requests?limit=N` | 已分析请求列表（默认 100，上限 1000） |
| `GET /api/v1/requests/{id}` | 请求头 |
| `GET /api/v1/requests/{id}/report` | 完整证据：视图、事件、冲突、报文元数据 |
| `GET /api/v1/requests/{id}/events?code=&level=&flow=&direction=&limit=` | 决策事件（可过滤） |
| `GET /api/v1/requests/{id}/conflicts?flow=&generation=&direction=` | 逐字节冲突证据 |
| `GET /api/v1/requests/{id}/packets` | 输入报文元数据（无载荷） |
| `GET /api/v1/requests/{id}/flows/{flow}/generations/{g}/stream/{direction}` | 回放原始连续字节 |

`{flow}` 必须 URL 编码，例如 `10.0.0.1%3A40001%3C-%3E10.0.0.2%3A80`（直接取 report 中的
flow 字符串编码即可）。`{direction}` 为 `a_to_b` 或 `b_to_a`。

流回放接口：
- 默认 `application/octet-stream`，**只含有证据的连续前缀**；响应头给出
  `X-Tcpreplay-Handshake-Known`、`X-Tcpreplay-Fin-Seen`、
  `X-Tcpreplay-Gap-Count`、`X-Tcpreplay-Held-Count` 等。
- 加 `?format=json` 返回完整 `DirectionView`：`stream`（base64）、`gaps`、
  `held_out_of_order`、`quarantined_bytes`、`fin_position`、`length_proved` 等。

## 冲突证据字段（conflicts 数组元素）

```json
{
  "conflict_id": "conf-000005",
  "request_id": "req-demo-001",
  "offered_record_id": "rec-00005",
  "flow": "10.0.0.1:40001<->10.0.0.2:80",
  "generation": 0,
  "direction": "a_to_b",
  "byte_offset": 10,
  "raw_seq": 1011,
  "accepted_byte": 82,
  "offered_byte": 122,
  "accepted_by_record_id": "rec-00004",
  "policy": "first_wins",
  "disposition": "rejected",
  "timestamp": "2023-11-14T22:13:20.004Z"
}
```

`disposition` ∈ `rejected`（first_wins）/ `replaced`（last_wins）/
`quarantined`（隔离，未入流）。

## 事件码

`SYN_OPENED` `SYN_DUPLICATE` `SYNACK_ESTABLISHED` `HANDSHAKE_ABSENT`
`NEW_GENERATION` `SEGMENT_ACCEPTED` `RETRANSMIT_IDENTICAL` `OVERLAP_CONFLICT`
`DATA_AFTER_FIN_REJECTED` `FIN_ACCEPTED` `FIN_DUPLICATE` `FIN_POSITION_CONFLICT`
`RST_CLOSED` `PACKET_AFTER_RST_UNDECIDED` `GENERATION_COMPLETE`

级别：`info` / `warn` / `reject` / `undecided`。
