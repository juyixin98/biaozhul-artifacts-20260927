# tcpreplay — 离线 TCP 双向流重组服务

从离线报文（pcap 文件或 JSON 报文列表）重组 TCP 双向字节流。只输出**有证据的连续区间**；
缺口、冲突、半关闭、跨 32 位回绕、连接复用（握手世代）都有独立的、可逐字节定位的证据记录。

## 快速开始（验收复现）

依赖：Go 1.23+（开发验证版本 go1.23.4）、SQLite 驱动 `modernc.org/sqlite v1.34.5`（纯 Go，无需 CGO，
版本见 `go.mod`/`go.sum`）。不需要生产账号、外部服务或真实业务数据。

```bash
# 1. 单元 + 场景 + 端到端测试（约 2 秒）
go test ./...

# 2. 生成合成夹具（7 个 pcap + 人工编写的期望值 sidecar）
go run ./cmd/genfixtures -out test/fixtures

# 3. 构建 CLI 并离线回放
go build -o tcpreplay ./cmd/tcpreplay
./tcpreplay replay --config configs/tcpreplay.json \
    --pcap test/fixtures/04-seq-wrap.pcap --request-id demo-wrap --out /tmp/out-wrap
cat /tmp/out-wrap/stream_10.0.0.1_40001-10.0.0.2_80_g0_a_to_b.bin
# 期望输出: HELLO-TCP-REASSEMBLY-WORLD!!

# 4. 启动 HTTP 服务并回放
./tcpreplay serve --config configs/tcpreplay.json &
curl -s -X POST "http://127.0.0.1:8080/api/v1/ingest/pcap?request_id=demo1" \
     --data-binary @test/fixtures/01-outoforder-gap.pcap
curl -s "http://127.0.0.1:8080/api/v1/requests/demo1/report" | python3 -m json.tool
```

每个夹具的期望结果（已知原始流、缺口区间、冲突字节）写在
`test/fixtures/*.expected.json`，由人工编写，**不**由被测实现生成。

## 目录结构

```
cmd/tcpreplay/        CLI：serve（HTTP 服务）与 replay（一次性离线分析）
cmd/genfixtures/      合成夹具生成器（独立小工具，不属于被测核心）
internal/netmodel/    网络模型：报文类型、32 位回绕序号空间、pcap 读写
internal/reassembly/  重组核心：世代/方向状态机、重叠策略、缺口与冲突证据（纯逻辑）
internal/storage/     SQLite 证据存储（请求、事件、冲突、报文元数据、世代视图）
internal/service/     HTTP API 与入库流水线（每次请求独立引擎实例）
internal/config/      JSON 配置 + TCPREPLAY_* 环境变量覆盖 + 校验
internal/diagnose/    结构化诊断日志（强制脱敏）
internal/testsupport/ 测试夹具构建器与独立 oracle（仅测试使用）
configs/              示例配置
test/fixtures/        生成的 pcap 夹具与期望值 sidecar
docs/                 架构与 API 文档
```

## 语义要点（审查清单对应）

| 要求 | 实现位置 | 验证 |
|---|---|---|
| 32 位回绕窗口语义 | `netmodel/seqspace.go`（`FwdDist` 用 `int32` 差值，锚点投影） | `TestFwdDistAndOrder`、`TestSequenceWrap`（ISN=0xFFFFFFF6，数据跨 0） |
| SYN/FIN 消耗序号 | SYN 占方向坐标 0、首字节为 1；FIN 位置 = seq+len(payload)，再占 1 | `TestOutOfOrderWithGaps`（FINPos=28 对应 28 字节流）、`TestHalfCloseAndDataAfterFIN` |
| 重传不重复输出 | `sparseBuffer.put` 的 identical 分支 | `TestRetransmissionNoDuplicateOutput` |
| 冲突重叠按策略隔离 | `first_wins`/`last_wins`/`quarantine` 三策略，逐字节证据 | `TestOverlapPolicies`（含 corrupt-first 时序） |
| 握手世代区分复用 | `latestOrNewGeneration`：新 SYN（不同 ISN 或前世代已关闭/RST）开新世代 | `TestConnectionReuse`、`TestRSTClosesGenerationAndPostRSTUndecided` |
| 缺包只输出有证据区间 | 连续前缀才输出；越序段单独列为 held 证据；缺口需后向证据或 FIN 证明 | `TestMissingSegmentProvesGap`、`TestOutOfOrderWithGaps` |
| 缺握手 | 无 SYN 时锚定首个观测字节，标记 `handshake_known=false` + undecided 事件 | `TestMissingHandshake` |
| 诊断带标识与关键状态 | 事件含 request_id/record_id/原始序号/相对坐标/next_contiguous/fin_pos | `TestEventsEndpointFiltering` |
| 敏感数据脱敏 | 日志默认不含 payload；预览需显式开启且截断 16 字节 | `TestRedactionDefaultsHidePayload` |

## 坐标约定

所有对外坐标（视图、冲突、事件）都是**相对该方向首个应用数据字节**的偏移，首字节为 0。
有 SYN 时 SYN 占其前一个序号（内部坐标 0）；无握手时 0 是首个观测到的数据字节。
原始 32 位序号在冲突与事件中同时携带（`raw_seq`），便于与抓包对照。

## 已知限制

- 序号投影基于锚点最近邻，单个方向内两次观测间隔超过 2^31 字节时存在理论歧义
  （这是 32 位序号空间的固有限制，文档见 `docs/ARCHITECTURE.md`）。
- 稀疏缓冲按字节存储，适合每方向数十 MB 级抓包；更大规模需换区间树（接口已隔离）。
- pcap 解析支持经典 libpcap 格式（Ethernet / raw IP / Linux cooked），不支持 pcap-ng。
- IP 分片不重装（记录并跳过）。
