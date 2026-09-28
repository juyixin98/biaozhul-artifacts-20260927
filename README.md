# dhcpv4lab — 本地 DHCPv4 报文与租约状态机子集

一个**完全本地、环回隔离**的 DHCPv4 教学/验证实现。报文范围限定为
**DISCOVER、OFFER、REQUEST、ACK、NAK、RELEASE**。所有网络参与者都是本机合成
夹具；默认配置下不监听、不发送任何非环回流量，也不绑定特权端口 67/68。

技术栈：Go 1.23（仅标准库做网络与 HTTP）+ SQLite（`modernc.org/sqlite`，纯 Go，
**无 CGO 依赖**）。独立参考预言机用 Python 3 标准库编写，不复用任何被测代码。

---

## 1. 边界语义（实现了什么 / 明确没实现什么）

### 1.1 报文子集
| 方向 | 报文 | 处理 |
|---|---|---|
| 客户端→服务器 | DISCOVER | 原子预留一个地址，回 OFFER |
| 服务器→客户端 | OFFER | 仅表示**预留**，不构成租约 |
| 客户端→服务器 | REQUEST | 三种情形分别处理（见 1.3），回 ACK 或 NAK |
| 服务器→客户端 | ACK | 租约原子提交 / 续租确认 |
| 服务器→客户端 | NAK | 明确拒绝类别，`yiaddr=0`，不带租约参数 |
| 客户端→服务器 | RELEASE | 按 RFC 2131 **无应答**；仅当前所有者可释放 |

DECLINE、INFORM 及未知 option-53 值被归类为 `unsupported_message`（不回复），
绝不会被当作成功。

### 1.2 关键状态语义
- **OFFER 预留 ≠ 已租用。** OFFER 只写一行 `OFFERED` 记录，有独立的
  `offerTTL`；到期后由清扫器转为 `EXPIRED`，地址回到池中。它**没有**
  `bound_at / ends`，也不会产生任何已承诺租期。
- **事务号（xid）与客户端身份联合关联。** 去重键是
  `(client-identity, xid, fingerprint)`，其中 identity 优先用 option 61
  client-id，缺省回退 chaddr；fingerprint 对报文语义字段（类型、ciaddr、
  option 50/54、secs、broadcast、chaddr、client-id）取哈希。
- **重复包可重答，但不延长未授权租期。** 指纹完全相同的重传：直接回放已存储的
  应答字节，**零状态变更**。续租只有在“已存在 BOUND 租约且未到期”时才会延长。
- **地址唯一分配 + 租约提交原子。** SQLite 部分唯一索引
  `WHERE state IN ('OFFERED','BOUND')` 同时保证“每地址最多一条活跃记录”和
  “每客户端最多一条活跃记录”；OFFERED→BOUND 的翻转在单个
  `SERIALIZABLE` 事务内完成，并发争抢由数据库裁决，提交时唯一约束冲突即失败。
- **异常/未知状态显式分类，绝不统一返回成功。** 类别包括：`ok`、`nak`、
  `no_reply`、`malformed_packet`、`unsupported_message`、`pool_exhausted`、
  `address_contention`、`internal_error`。每个判定都落一条结构化事件。

### 1.3 REQUEST 的三种情形（RFC 2131 §4.3.6 / §4.4）
通过 `option 54(server-id)`、`ciaddr`、`option 50(requested-ip)` 区分：

| 情形 | 判定特征 | 成功条件 | 结果 |
|---|---|---|---|
| **SELECTING（服务器选择）** | 有 server-id，ciaddr=0 | server-id 必须是本机；该地址存在属于本客户端的 OFFERED 预留 | OFFERED→BOUND，回 ACK；否则 NAK；选错服务器则静默 |
| **INIT-REBOOT（客户端重启）** | 无 server-id，ciaddr=0，有 option 50 | 该地址有本客户端**未到期的 BOUND 租约** | 回 ACK，但沿用**原租约边界，不延长**；否则 NAK |
| **RENEWING/REBINDING（续租/重绑）** | ciaddr≠0，无 server-id | ciaddr 处有本客户端未到期的 BOUND 租约 | 从“当前时刻”重新计算租期并延长（renew_count+1）；否则 NAK |

同一帧同时出现 server-id 与非零 ciaddr 属畸形 REQUEST，回
`nak_malformed_request`。

### 1.4 明确不在范围内（未执行的能力，不声称已实现）
- 不实现 DECLINE 的地址冲突探测、INFORM、BOOTP 中继（giaddr 非零即拒）。
- 不实现 DHCPv6、DNS 动态更新、租约持久化文件以外的任何外部集成。
- 环回实验室传输**不模拟**真实链路层广播：无论 broadcast 标志为何，应答都
  单播回数据报来源地址（broadcast 标志仍被保留与断言）。
- T1/T2 只作为 option 下发给客户端，服务器本身不区分 renew 与 rebind
  （二者在服务器侧语义一致，都是对未到期 BOUND 租约的延长）。

---

## 2. 工程组织

```
cmd/dhcpd/                守护进程：加载配置 → SQLite → 状态机 → UDP + HTTP
internal/
  config/                 JSON 配置、环境变量覆盖、环回安全校验
  dhcppacket/             RFC 2131 子集的报文编解码（独立可用，含自有字节向量测试）
  storage/                SQLite schema、原子事务、部分唯一索引、去重表、事件表
  server/                 传输无关的状态机核心（DISCOVER/REQUEST/RELEASE 分支）
  transport/              环回 UDP 适配 + HTTP 回放/诊断 API
  version/                版本元数据
configs/
  example.json            安全默认示例（testMode=false）
  test.json               测试夹具配置（环回、假时钟、每次重置库）
fixtures/
  dhcp_oracle.py          独立 Python 参考预言机（自实现编解码，不依赖被测代码）
scripts/
  start_test_server.sh    构建并启动环回测试服务器（假时钟）
  stop_test_server.sh
  verify_all.sh           一键验证：vet → race 测试 → 构建 → 独立预言机
```

状态机核心 `server.Handle(decodedPacket, source)` 与传输解耦，因此 **UDP
原始套接字夹具**与 **HTTP `/api/replay`** 驱动的是同一份核心逻辑。

### 持久化 schema（节选）
- `leases`：`UNIQUE(ip, identity_id)` + 两个部分唯一索引（活跃地址、活跃客户端）。
- `transactions`：`UNIQUE(identity_id, xid, fingerprint)`，存原始应答字节用于重放。
- `events`：每条判定一行（runId / xid / identity / 类别 / reason / 详情）。

SQLite 以 WAL、`synchronous=FULL`、单写连接、5s busy_timeout 运行。

---

## 3. 快速开始

要求：Go 1.23+、Python 3.10+（仅运行独立预言机时需要）、SQLite 由 Go 驱动内嵌。

```bash
# 查看默认配置
go run ./cmd/dhcpd --print-defaults

# 一键完整验证（推荐先跑这个）
./scripts/verify_all.sh

# 或手动分步
./scripts/start_test_server.sh
python3 fixtures/dhcp_oracle.py \
  --udp 127.0.0.1:10067 --http http://127.0.0.1:18080 \
  --run-id manual --report oracle_report.json
./scripts/stop_test_server.sh
```

`go test -race -count=1 ./...` 覆盖报文编解码、存储原子性、状态机场景与真实
环回 UDP 集成。

### 配置覆盖（环境变量）
`DHCPV4LAB_UDP_LISTEN`、`DHCPV4LAB_HTTP_LISTEN`、`DHCPV4LAB_DSN`、
`DHCPV4LAB_TEST_MODE`、`DHCPV4LAB_ALLOW_NON_LOOPBACK`、`DHCPV4LAB_RUN_ID`、
`DHCPV4LAB_FAKE_CLOCK`（仅 testMode）、`DHCPV4LAB_JSON_LOG`。

默认配置强制 `udpListen/httpListen` 与地址池都在 `127.0.0.0/8`；要解除必须显式
设置 `allowNonLoopback=true`（自行承担风险，测试脚本从不设置它）。

---

## 4. HTTP 回放与诊断接口

| 方法路径 | 用途 |
|---|---|
| `GET /healthz` | 存活与 runId |
| `GET /version` | 版本/commit/Go 版本 |
| `POST /api/replay` | 注入 base64/hex 报文，返回结构化判定与应答（与 UDP 同核心） |
| `GET /api/leases?state=&ip=&identity=&labelContains=` | 租约状态 |
| `GET /api/pool` | OFFERED/BOUND 计数与当前时钟 |
| `GET /api/events?runId=&all=1&order=asc` | 结构化判定事件 |
| `GET /api/counters` | accepted/rejected/replayed 计数 |
| `POST /test/clock/advance` | 仅 testMode：推进假时钟 |
| `POST /test/sweep` | 仅 testMode：执行一次过期清扫 |
| `POST /test/reset` | 仅 testMode：清空状态并把假时钟复位 |

NAK 在协议上是有效应答，因此 `/api/replay` 返回 HTTP 200 并在 body 里给
`category:"nak"`；只有传输层都无法解析的报文返回 422 +
`category:"malformed_packet"`。

---

## 5. 测试与诊断

### 5.1 测试如何关联“输入 / 运行身份 / 判定依据”
- 每个服务器进程有唯一 `runId`（可用 `DHCPV4LAB_RUN_ID` 固定），写入每条日志与
  事件；每个事件都带 `xid`、`identityId`、源地址、输入/输出类型、
  `action/result/reason/detail`。
- 预言机的每条 `PASS/FAIL` 打印步骤、期望值、观测值与判定依据，并生成
  `oracle_report.json`。
- 服务器启动日志输出版本 banner；`/version` 与报告都记录版本，便于把一次运行
  关联到具体代码版本。

### 5.2 独立 Go 测试断言具体结果（而非“接口能调用”）
- 报文层：独立逐字节构造的真值向量，校验 DISCOVER/REQUEST 解码，并对
  截断、坏 magic、hlen 错误、选项越界、缺 END、END 后杂字节等断言具体错误。
- 存储层：24 个 goroutine 争抢同一地址必须**恰好 1 个赢家**；续租改 owner
  断言；OFFER/BOUND 到期清扫；释放的属主校验与幂等；去重表回放。
- 状态机：DORA、无 OFFER 即 NAK、选错服务器静默、重复包字节级回放且租期不变、
  续租延长但 INIT-REBOOT 不延长、过期续租 NAK、小池并发唯一分配、争用失败者
  NAK、过期 OFFER 后陈旧 REQUEST 被 NAK、RELEASE 属主/陌生人/重复语义、畸形与
  不支持报文分类。
- 传输层：真实环回 UDP 套接字上的 DORA、畸形帧静默、4 客户端并发端到端、池耗尽
  后 RELEASE 再分配。

### 5.3 独立预言机（参考答案不由被测核心生成）
`fixtures/dhcp_oracle.py` 用 Python 标准库**另行实现** DHCP 编解码，通过真实
UDP 与 HTTP 驱动正在运行的服务器，覆盖：DORA、重复不延期、旧 xid 重放被
NAK、INIT-REBOOT 不延期、假时钟过期+清扫+地址再分配、4 路并发唯一归属、
RELEASE 属主保护与幂等、畸形/不支持报文不产生成功应答，以及事件与 run/xid 的
可关联性。它默认先调用 `/test/reset`，因此可对同一服务器重复执行。

---

## 6. 已知限制 / 无法在此环境执行的检查
- 未在真实 DHCP 中继或跨主机二层网络上验证——这是刻意的环回边界，不属于本次
  范围；相关广播/giaddr 行为已在“未实现”中列明。
- 未做压测或长稳 soak test；并发正确性以 `-race` 与有限夹具证明，不代表大流量
  性能结论。
- 依赖版本在 `go.mod`/`go.sum` 固定；离线构建需要这些模块已在本机模块缓存中
  （交付环境已验证 `go mod verify` 通过）。
