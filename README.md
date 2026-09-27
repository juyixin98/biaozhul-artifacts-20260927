# tcpreasm — 离线 TCP 双向流重组（保留缺口与冲突证据）

把离线抓包（合成的 JSONL/JSON 报文）重组为每条 TCP 连接**两个方向**的字节流，
并且：

- 序号严格按 **32 位回绕窗口语义**处理；**SYN 与 FIN 各消耗一个序号**；
- **重传相同数据不重复输出**；矛盾重叠按声明策略隔离，**已投递字节不可变**；
- 连接复用用**握手世代（generation）**区分，不同世代的字节绝不串流；
- 缺包时**只输出有证据的连续前缀**，缺口逐字节记录，不补造任何内容；
- 每个接受/拒绝/无法判定都带**请求标识、报文标识和关键序号状态**，负载只打印
  长度 + SHA-256（可选脱敏预览）。

所有数据都是本地合成夹具，无外部账号、无真实业务数据。

## 目录结构（模块有真实职责）

```
cmd/tcpreasm            serve（HTTP 服务）/ replay（离线回放）两个入口
cmd/genfixtures         由独立参考答案生成器重新生成 testdata/
internal/config         配置解析：默认值 < JSON 文件 < TCPREASM_* 环境变量
internal/tcpmodel       网络模型：报文、四元组、方向、JSONL 抓包格式、SEG.LEN
internal/reassembly/seqnum  32 位回绕窗口算术（Compare/Sub/Extend）
internal/reassembly     重组核心：世代状态机、乱序缓冲、重叠/冲突、投递、FIN
internal/store          SQLite 证据库：连接/世代/报文台账/流块/缺口/冲突/诊断
internal/httpapi        回放与证据查询接口（仅标准库 net/http）
internal/diag           决策记录、负载指纹与脱敏
internal/oracle         独立参考答案：理想 TCP 发送端模型（不依赖被测核心）
internal/fixture        基于 oracle 的夹具构造与黄金答案
testdata/               11 个合成夹具（.jsonl + .expected.json）
configs/                示例配置与请求样例
```

**参考答案独立性**：`internal/oracle` 是一个独立的“理想发送端”线性模型，
黄金答案（原始字节流、缺口偏移、冲突两侧 SHA-256）由它生成；
`internal/fixture` 不导入 `internal/reassembly`（编译期保证），测试用黄金答案
断言被测核心，答案不可能由被测实现自身产生。

## 依赖

- Go 1.23（在 go1.23.4 linux/amd64 验证）
- SQLite 驱动：`modernc.org/sqlite v1.34.5`（**纯 Go，无需 CGO**）
- 其余仅标准库。`go.mod`/`go.sum` 已固定版本，可 `GOPROXY=off` 离线构建。

## 快速开始

```bash
# 构建
go build ./...

# 全部测试（含 -race 可选）
go test ./...
go test -race ./...

# 重新生成合成夹具（通常无需，testdata 已入库）
go run ./cmd/genfixtures -out testdata

# 离线回放一个夹具，结果 JSON 写到 stdout，逐包判定写 stderr
go run ./cmd/tcpreasm replay -config configs/example.json testdata/missing_segments.jsonl
```

启动 HTTP 服务：

```bash
go run ./cmd/tcpreasm serve -config configs/example.json
# 另一个终端：
curl -s -X POST localhost:18080/v1/ingest \
  -H 'Content-Type: application/json' --data @configs/request_sample.json
curl -s "localhost:18080/v1/connections"
```

## HTTP 接口

| 方法/路径 | 说明 |
|---|---|
| `POST /v1/ingest` | 批量 JSON：`{"request_id": "...", "packets": [...]}` |
| `POST /v1/ingest/jsonl` | 原始 JSONL 抓包（每行一个报文，`#` 开头为注释），请求 id 用 `X-Request-Id` |
| `GET /v1/connections` | 所有连接与握手世代 |
| `GET /v1/stream?flow_key&gen&direction(c2s|s2c)&start&end&format=hex|raw` | 已重组的连续字节；缺口之后不返回 |
| `GET /v1/gaps?flow_key&gen&direction&status(open|filled)` | 缺口证据 |
| `GET /v1/conflicts?flow_key&gen&direction` | 冲突台账（偏移、两侧 SHA、策略、赢家） |
| `GET /v1/diagnostics?request_id&flow_key&gen&category&limit` | 决策记录 |
| `GET /healthz` | 健康检查 |

报文为 JSONL/JSON，负载用十六进制 `payload_hex`；标志位 `syn/ack_flag/fin/rst`。
同一 `(source, record_id)` 重复摄入会被标记 `duplicate_packet`，不产生重复字节。

## 配置（`configs/example.json`）

```json
{
  "http":  {"addr": "127.0.0.1:18080"},
  "storage": {"dsn": "data/tcpreasm.db"},
  "reassembly": {
    "overlap_policy": "first-wins",
    "max_buffered_bytes_per_dir": 8388608,
    "delivered_evidence_bytes": 262144,
    "infer_generation_without_handshake": true
  },
  "diagnostics": {"payload_preview_bytes": 0, "mask_ips": false}
}
```

- `overlap_policy`：`first-wins`（默认，丢弃矛盾新字节）/ `last-wins`
  （替换尚未投递的在位字节）/ `quarantine`（两份都留证、区间隔离不投递）。
  **任何策略下已投递字节都不可变**，针对它们的矛盾重传记为
  `UNDECIDABLE_CONFLICT_AGAINST_DELIVERED`。
- 环境变量覆盖：`TCPREASM_OVERLAP_POLICY`、`TCPREASM_HTTP_ADDR`、
  `TCPREASM_STORAGE_DSN`、`TCPREASM_INFER_NO_HANDSHAKE`、
  `TCPREASM_PAYLOAD_PREVIEW_BYTES`、`TCPREASM_DIAG_MASK_IPS` 等。

## 关键语义与判定类别

- 回绕：32 位序号先 `Extend` 提升到 64 位绝对空间（以 rcv.nxt 为窗口锚点），
  比较用模 2³² 的有符号距离；SYN 占 ISN、首字节为 ISN+1，FIN 占数据末尾序号。
- 世代：新 SYN 开启世代；只有上一世代双向关闭/复位后新 SYN 才算复用，
  同 ISN 的重传 SYN 计为证据不重开；数据报文按窗口距离**打分绑定**世代，
  两个世代同等可信时返回 `UNDECIDABLE_GENERATION_AMBIGUOUS` 而不猜测。
- 缺握手：数据先于任何 SYN 到达时，若允许推断则建立显式标记
  `inferred=true` 的世代（绝对偏移不可证），否则拒绝。
- 代表性类别：`ACCEPTED_{IN_ORDER,OUT_OF_ORDER}_DATA`、
  `ACCEPTED_RETRANSMIT_IDENTICAL`、`REJECTED_DATA_AFTER_FIN`、
  `REJECTED_OUTSIDE_WINDOW`、`REJECTED_CONFLICT_FIRST_WINS`、
  `REJECTED_CONFLICT_LAST_WINS_REPLACED_INCUMBENT`、
  `UNDECIDABLE_CONFLICT_QUARANTINED`、`UNDECIDABLE_GAP`。

## 测试夹具（与原始流对照、逐字节定位）

| 夹具 | 覆盖点 |
|---|---|
| `in_order` | 基线双向重组、SYN/FIN 消耗序号 |
| `out_of_order` | 全逆序到达，结果与原始流逐字节相同且无重复输出 |
| `retransmission` | 相同重传去重，不二次输出 |
| `conflicting_retransmission` | 矛盾重传打在**已投递**字节：定位 [10,11)，不可变 |
| `overlap_conflict` | 乱序缓冲期重叠冲突 [16,20)，三种策略分别断言 |
| `missing_segments` | 每方向丢一个中间段：c2s 缺口 [30,45)、s2c [15,30) |
| `wrap_boundary` / `wrap_boundary_gap` | 跨 2³² 回绕重组 / 回绕点缺口偏移正确 |
| `half_close` | 半关闭后越界数据 `REJECTED_DATA_AFTER_FIN` |
| `missing_handshake` | 无握手数据进入 inferred 世代 |
| `connection_reuse` | 同一四元组两次握手：2 个世代，字节互不串流 |

测试断言具体结果与失败类别（偏移、SHA、决策枚举），不是“接口可调用”。

## 一键复现

```bash
./scripts/reproduce.sh
```

从干净状态构建、跑全部测试、用三种策略回放夹具并汇总缺口/冲突，结果写入
`reproduce-results/`。
