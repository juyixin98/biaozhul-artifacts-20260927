# natlab — 本地可回放的有状态 NAT 模型

`natlab` 是一个**纯模型**：吃进报文元数据（五/六元组 + TCP 标志位 + 观测时间），
吐出映射后的五元组和一条带分类理由的决策记录。它**从不打开任何真实网络套接字
转发报文**，唯一的网络行为是可选的、仅绑定 `127.0.0.1` 的本地回放 HTTP 接口。

所有地址均为合成夹具（`10.0.0.0/8` 内网，`198.51.100.0/24`、`203.0.113.0/24`
TEST-NET 文档地址）。不需要任何生产账号或真实业务数据。

## 能力与边界

- **有状态 NAPT（对称型）**：一条内部流（完整五元组）独占一个外部端口；返回包
  必须命中该映射，且来源必须是建流时的确切远端端点，否则按类拒绝。
- **TCP / UDP 超时分开**：TCP 区分 `SYN_SENT / ESTABLISHED / FIN_WAIT_1/2 /
  TIME_WAIT` 各自独立超时；UDP 单独一个空闲超时。
- **端口分配**：按协议各自独立端口池；活跃映射占用的端口绝不复用；支持释放后
  冷却窗（`reuse_cooldown`）。分配取区间内**最小空闲端口**，保证回放确定性。
- **返回包必须匹配连接状态**：未建映射、映射已过期、远端不匹配、TCP 状态非法
  是四类**可区分**的拒绝。
- **仅接受已重组输入**：`fragmented=true` 一律拒绝；ICMP 等协议拒绝。
- **单调时钟**：模型维护只增不退的水印（watermark）。时间戳回拨的报文按当前
  水印评估，**不能让已过期连接复活**。
- **可回放**：每次决策（run id、序号、映射前后五元组、状态前后、水印、中间过期
  事件、判断理由）都写入内存或 SQLite；关掉进程用同一个 DB 文件可以续跑。

## 工程结构（模块边界与数据/错误契约）

```
cmd/natlab/            程序入口：replay（跑夹具出报告）/ serve（本地 HTTP）
internal/model/        跨模块数据契约：Packet / FiveTuple / Decision / 错误分类
internal/config/       配置解析与校验（JSON + NATLAB_* 环境变量覆盖）
internal/portalloc/    每协议独立端口池：不复用活跃端口、冷却、确定性分配
internal/storage/      持久化边界 Store 接口 + memory / sqlite / 故障注入实现
internal/nat/          网络模型核心：校验、惰性过期、映射、TCP 状态机、恢复
internal/replay/       追踪文件解析、Runner 批量执行、loopback HTTP 接口
configs/natlab.json    启动配置样例
traces/*.json          合成报文追踪夹具（期望结果在测试里手写，见下）
tests/integration/     端到端：夹具逐包断言、SQLite 跨进程续跑、HTTP
reports/ logs/         生成产物（决策报告、带运行编号的测试日志）
```

错误契约（`internal/model/model.go`）——每个拒绝**恰好**落入一类：

| Class（粗类）            | Reason（细因，逐包断言）                                                        |
|--------------------------|--------------------------------------------------------------------------------|
| `input_error`            | `invalid_input` `protocol_unsupported` `fragment_not_reassembled` `external_address_mismatch` |
| `state_conflict`         | `no_matching_mapping` `mapping_expired` `remote_endpoint_mismatch` `tcp_state_conflict` |
| `resource_exhaustion`    | `port_pool_exhausted`                                                          |
| `compute_failure`        | `storage_failure`（持久化失败；与策略拒绝严格区分，返回 Go error + 决策双记录） |

## 快速开始

需要 Go 1.23+。SQLite 使用纯 Go 驱动 `modernc.org/sqlite`，**不需要 cgo**。

```bash
go build -o bin/natlab ./cmd/natlab

# 回放一个夹具，决策报告打印到 stdout
./bin/natlab replay --config configs/natlab.json --trace traces/01_bidirectional.json
```

持久化到本地 SQLite 并在进程关闭后续跑（`sqlite_path` 也可写进配置）：

```bash
export NATLAB_SQLITE_PATH=$PWD/natlab.db
./bin/natlab replay --trace part1.json --out reports/part1.json
./bin/natlab replay --trace part2.json --out reports/part2.json   # 同一 DB，映射仍在
```

启动仅监听 loopback 的回放 API：

```bash
./bin/natlab serve                       # 默认 127.0.0.1:18080
curl -s http://127.0.0.1:18080/healthz
```

## 运行测试（真实命令与结论）

```bash
make test      # = go test -count=1 ./internal/... ./tests/...
make race      # -race 全量
make cover     # 覆盖率到 reports/coverage.out
make fixtures  # 回放全部 traces/*.json，逐 run 的完整决策写入 reports/
```

最近一次执行结论（环境：linux/amd64, go1.23.4；完整带时间戳日志在 `logs/`）：

```
$ go test -race -count=1 ./...
ok  	natlab/internal/config           1.016s
ok  	natlab/internal/nat              1.026s
ok  	natlab/internal/portalloc        1.014s
ok  	natlab/internal/storage          1.201s
ok  	natlab/tests/integration         1.187s
```

核心模型 `nat.(*Engine).Process` 语句覆盖率 100%，入站处理 95%，端口分配器
分配/释放关键路径 100%（`go tool cover -func` 可复现）。

### 需求场景 → 测试对照表（期望全部手写，不由被测实现生成）

参考答案以两种独立形式存在，均不调用被测核心来"生成期望"：

1. 单元测试里**字面量期望值**（端口号、状态名、拒绝原因）；
2. 集成测试里针对每个夹具**手写的逐包期望表** `expect{verdict,reason,postSrc,…}`。

| 需求验证点                 | 测试                                                                | 断言的具体结果 |
|----------------------------|---------------------------------------------------------------------|----------------|
| 端口耗尽                   | `TestPortExhaustion`、`TestExhaustionAndReuseAfterRelease`、夹具 02 | 第 3 个流 `port_pool_exhausted`/`resource_exhaustion`，前两个端口 20000/20001 唯一 |
| 双向流                     | `TestBidirectionalFlow`、夹具 01                                    | 出站改源为 `198.51.100.1:20000`；返回改目的为 `10.0.0.2:40001` |
| 超时后复用（TCP/UDP 分开） | `TestTimeoutThenReuse`、`TestSeparateTCPAndUDPTimeouts`、夹具 03/04 | UDP 10s 过期、TCP established 30s 仍活；释放端口被新流拿到 30000/20000 |
| 迟到返回包                 | `TestNoMappingLateOrNever`、`TestTimeoutThenReuse`、夹具 03/04      | 近期死映射 `mapping_expired`，从未有映射 `no_matching_mapping`，二者不同 |
| 并发首包                   | `TestConcurrentFirstPackets`（-race 下运行）                        | 4 流并发得 4 个不同端口；池满后 8 个并发首包**全部**耗尽拒绝，零重复分配 |
| 一一对应                   | 上列耗尽/双向/并发测试                                              | 活跃映射 ↔ 流 ↔ 端口一一对应，活跃端口绝不二次分配 |
| 拒绝原因可区分             | `TestInputAndConflictClasses`、夹具 05                              | input / state / exhaustion / compute 四类及细因逐字断言 |
| 时间回拨不复活             | `TestClockRollbackDoesNotRevive`、夹具 03 seq7                      | 回拨包水印不变、映射仍死、`clock_rollbacks=1` |
| 计算失败独立成类           | `TestComputeFailureClass`（故障注入存储）                           | verdict=`compute_failure`、Go error 非空、失败写入回滚不泄漏端口 |
| 不改系统真实网络           | 全部测试无任何 `net.Dial`/`net.Listen` 转发；HTTP 仅 `httptest`/loopback | — |

## 决策日志：如何重放一个问题

每条 `Decision` 保留可重放所需的全部中间状态：

```jsonc
{
  "run_id": "r-timeout-reuse-01", "seq": 4, "label": "udp-return-A-after-timeout-late",
  "at": "2026-01-01T00:00:20Z", "verdict": "reject",
  "reason": "mapping_expired", "class": "state_conflict",
  "pre":  { "src_ip": "198.51.100.200", "src_port": 53, "...": "..." },
  "state_before": "UDP_OPEN", "state_after": "",
  "watermark": "2026-01-01T00:00:20Z",
  "rationale": "late return on port 30000 from 198.51.100.200:53: mapping already expired/closed"
}
```

过期瞬间另有 `verdict:"lifecycle_expired"` 的生命周期事件（含状态前后）。
用 `run_id` + `seq` 即可在 `reports/<fixture>.json` 或 SQLite `decisions` 表中
定位完整前因后果；用同一追踪文件重放得到逐字节一致的结果。

## 夹具说明（traces/）

| 文件 | 场景 |
|------|------|
| `01_bidirectional.json` | TCP 握手双向转发 + UDP 问答 + 伪远端拒绝 + 未请求端口拒绝 |
| `02_exhaustion.json`    | 2 端口 TCP 池：第三个流耗尽，已有流重传仍可用 |
| `03_timeout_reuse.json`  | 单端口 UDP 池：活跃期占用→超时→迟到包→新流复用→过去时间戳包不复活 |
| `04_tcp_close.json`     | 完整 FIN 挥手穿越 FIN_WAIT/TIME_WAIT，TIME_WAIT 非法包拒绝，关后释放复用 |
| `05_input_rejects.json` | 分片、ICMP、坏地址、零端口、无标志 TCP、SYN+FIN、坏方向、外部地址不符 |

追踪为单个 JSON 对象（`run_id` 必填，`config` 可内嵌覆盖），也支持 JSONL
（首行头、后续每行一个 packet）。

## HTTP 回放接口

```
POST /v1/runs                       {"run_id":"r1"}            新建运行
POST /v1/runs/{id}/packets          <Packet>                   喂一个包，返回 Decision
GET  /v1/runs/{id}/decisions?from_seq=&limit=                   决策日志
GET  /v1/runs/{id}/mappings                                     当前活跃映射快照
GET  /v1/runs/{id}/stats                                        计数器
```

错误响应统一信封 `{"ok":false,"error":{"class","reason","detail"}}`，
`class` 即上面的四类之一。

## 设计取舍

- 对称型 NAPT 而非全锥型：返回包强制远端端点匹配，状态冲突最明确，便于教学
  与断言；需要其它锥形行为时改 `processInboundLocked` 的远端校验即可。
- 惰性过期而非后台 goroutine：时间完全由输入时间戳驱动，回放与并发都确定。
- `modernc.org/sqlite` 纯 Go 驱动，无 cgo/编译器依赖，单写者连接避免锁意外。
