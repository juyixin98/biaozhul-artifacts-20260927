# dhcp4lab — 本地 DHCPv4 报文与租约状态机子集

一个仅用于本地实验/测试的 DHCPv4 服务器子集实现（Go + 标准库网络/HTTP + SQLite）。
报文范围限定为 **DISCOVER / OFFER / REQUEST / ACK / NAK / RELEASE**（RFC 2131/2132）。
默认只绑定环回地址高端口；没有生产账号、没有真实业务数据、**不监听任何生产网卡**。

- 传输：标准 UDP socket（**非 raw socket**），默认 `127.0.0.1:10067`
- 诊断/回放：标准库 HTTP，默认 `127.0.0.1:18067`
- 持久化：SQLite（`modernc.org/sqlite` 纯 Go，无 cgo；版本固定并已 `vendor/`）
- 工程分层：配置解析 → 线协议模型 → 地址池 → 状态存储（事务）→ 协议适配 → UDP/HTTP 接口

## 目录结构

```
cmd/dhcp4d/            程序入口（装配各层）
internal/
  config/              JSON 配置层 + 全量校验（环回/端口/地址包含关系等）
  dhcp4/               BOOTP/DHCP 线模型：编解码、选项、客户端身份、分类解析错误
  ippool/              可分配地址区间枚举与纯函数选址
  storage/             SQLite 状态机：所有判定+落库在单个 IMMEDIATE 事务内
  server/              协议适配：REQUEST 三态分类、OFFER/ACK/NAK 编码
  udpserver/           环回 UDP 传输，带 run_id/seq/xid 的结构化日志
  replay/              HTTP /inject 回放与 /leases /journal /events 诊断
  version/             版本标识（日志与接口都会带上）
testfixture/
  wirekit/             独立参考编解码器（不 import 被测实现）
  dhclient/            合成客户端（真实环回 UDP，独立分类失败原因）
  testlog/             结构化、可关联 run_id/xid/chaddr/step 的测试日志
  testhelp/            假时钟、隔离内存库、环回服务装配
integration/           真实 UDP 环回端到端测试
configs/lab.json       默认配置
scripts/verify.sh      一键验证（离线构建 + vet + 全量 race 测试 + 真实冒烟）
scripts/smoke.sh       真实进程冒烟（Python 独立组包，断言具体结果）
```

## 快速开始

```bash
# 完全离线可构建（依赖已固定在 go.sum 并随仓库 vendor/）
./scripts/verify.sh

# 或手动：
go build -mod=vendor ./...
go test  -mod=vendor -race -count=1 ./...

# 启动（默认环回）：
go run -mod=vendor ./cmd/dhcp4d -config configs/lab.json -text-logs
```

`test-results/` 下会产生：逐包 `go test -v` 输出、JSONL 测试日志
（`TESTLOG_FILE`）、冒烟日志和构建产物。

## 核心状态机语义（边界）

### 身份与事务关联
- 客户端身份（`dhcp4.ClientIdentity`）：优先使用选项 61（raw type+value，命名空间
  前缀 `oid:`），否则使用 htype+chaddr（前缀 `mac:`），两种形式永不冲突。
- 去重键是 **身份 + XID + 接收报文类型**。同一 XID 在不同身份之间互不影响。
- 重放判定只看持久化的 `replies` 日志，进程重启后仍有效。

### DISCOVER / OFFER（预留 ≠ 租用）
- OFFER 在 `offers` 表产生一行**预留**（`superseded=0`，带 `expires_at`），
  **不会**在 `leases` 表产生任何行。
- 每个地址至多一条有效预留（部分唯一索引），每个客户端至多一条有效预留。
- 新的 DISCOVER（新 XID）会使旧预留失效；地址仍空闲时，按 RFC “SHOULD 保持一致”
  优先再次提供同一地址；其次优先客户端上次租用的地址；再做轮询扫描。
- 预留超过 `offer_ttl` 后：由“触碰即清扫（sweep on touch）”或后台 reaper 标记
  `superseded=1`，地址可被他人预留。
- 地址池耗尽返回明确的 `pool_exhausted`（不答复、不捏造地址）。

### REQUEST 的三种情形（显式区分）
适配层 `server.ClassifyRequest` 严格分类：
| 情形 | 判据 | 服务器行为 |
|---|---|---|
| SELECTING 服务器选择 | 有选项 54（且有 50、ciaddr=0） | 54 不是本服务器→**静默丢弃**；存在匹配的有效 OFFER（同身份/同 XID/同 IP/未过期）→提交租约 ACK；否则 **NAK**，细分 `no_active_offer_for_server_id` 与 `offer_does_not_match_requested_ip` |
| INIT-REBOOT 客户端重启 | 有选项 50、无 54、ciaddr=0 | 身份存在且其 live 租约正是请求 IP→ACK 并**重开租约计时**；IP 不符→NAK（`init_reboot_requested_ip_not_leased_to_client`）；身份无 live 租约（含已过期）→**静默**（`init_reboot_unknown_client_silent`），客户端须重新 DISCOVER |
| RENEW 续租 | ciaddr≠0、无 54 | ciaddr 与该客户端 live 租约一致→延长；无 live 租约→**静默**（`renew_without_active_lease_silent`）；ciaddr 与租约不符→NAK（`renew_ciaddr_does_not_match_lease`） |

结构性非法 REQUEST（如有 54 无 50、有 54 且 ciaddr≠0、三者皆无）归类为
`malformed_request`，回 NAK 让客户端回到 INIT，而不是无限重试坏报文。

### 地址唯一分配与租约原子提交
- 每个地址至多一条 `state='leased'` 行；每个客户端至多一条 live 租约
  （均为 SQLite 部分唯一索引强约束）。
- 选中新地址时，旧 live 租约在同一事务内先置为 `released`。
- OFFER→租约的转换（预留失效 + 旧租约结束 + 新租约插入）与回复日志写入
  **全部在一个 `BEGIN IMMEDIATE` 事务**内提交；并发争抢由数据库写锁串行化，
  地址不可能同时租给两个客户端（有 25/40 客户端并发 + `-race` 测试）。

### 重复包：可重答，但绝不延长未授权租期
- 同一（身份，XID，类型）重复：DISCOVER/ACK/NAK 可再次答复（重复响应）。
- 去重回复通过 `replies.ref_kind/ref_id` **精确钉到它当初授予的那一条
  offer/lease 行（按“代”）**，而不是只按（客户端，IP）。这样即使同一地址后来
  产生了新一代预留/租约，旧 XID 的重放也不会引用到新一代：
  - 重复 **ACK** 只在其所指的那条租约行仍为 `leased` 时重放；行已
    released/expired 即判定该去重行为 stale，旧 XID 按全新事务处理（通常 NAK），
    不会“借”同 IP 的新租约发出幽灵 ACK。
  - 重复 **OFFER** 只在其所指的那条 offer 行仍 live 时重放；新 DISCOVER 取代旧
    offer（即使又给了同一地址）后，旧 XID 重放会作为全新事务预留当前有效地址。
  - 重复 **NAK** 只在当初导致 NAK 的否定条件仍成立时重放；一旦成功前提出现
    （SELECTING 出现匹配有效 offer；INIT-REBOOT/RENEW 出现匹配 live 租约），该
    NAK 立即失效，后续合法 REQUEST 可正常 ACK（旧 NAK 不会“毒化”同 XID）。
- 重复 **ACK** 携带的是**原始到期时刻**：不更新 `leases.expires_at`，选项 51
  如实报“剩余时间”（可为 0，绝不向上伪造租期）而非全新租期；测试断言重放前后
  到期时刻完全一致。
- stale 的去重行被改记为 `action || '_stale'` 离开部分唯一索引，使同 XID 能作为
  新事务插入；历史仍保留在日志中。NAK 的有效重放永远重复 NAK。

### RELEASE 与过期
- RELEASE 无任何回复（RFC 2131）。匹配的 live 租约置 `released`，地址立即释放。
- `ciaddr=0.0.0.0` 的 RELEASE 是畸形报文：在适配层与存储层双重拦截，归类
  `malformed_request: release_without_ciaddr` 静默丢弃（绝不 panic；UDP 每报文
  处理另有 panic 隔离，单个报文无法拖垮服务，有真实环回崩溃隔离测试）。
- 对不属于本客户端 live 租约的 RELEASE（含二次释放）→ 静默并记录明确类别
  `release_ciaddr_not_leased_to_client`，**不会**被报成成功。
- live 租约超过 `lease_time` → 清扫为 `expired`；过期后续租/重启确认静默，
  地址回到池中；原客户端重新 DISCOVER 时按 SHOULD-reuse 优先拿回旧地址。

### 报文健壮性
- 独立分类的解析错误（稳定 code）：`message_too_short`、`bad_magic_cookie`、
  `bad_op`、`bad_htype_hlen`、`truncated_option`、`option_exceeds_message`、
  `missing_message_type`、`malformed_message_type`、`unsupported_message_type`、
  `malformed_server_id`、`malformed_requested_ip`、`malformed_client_id`、
  `missing_chaddr`。这些报文一律不回复，UDP 层以 WARN 记录类别。
- 非以太网（htype≠1 或 hlen≠6）、giaddr≠0 的中继场景在本子集拒绝
  （线模型仅放行以太网；中继未实现，见下）。

## HTTP 回放/诊断接口

- `GET  /api/v1/healthz`、`GET /api/v1/version`
- `POST /api/v1/inject`  body `{"datagram_hex": "<完整 BOOTP 报文十六进制>"}`
  返回 `action` / `reply_type` / `reply_hex` / `lease_ip` / `lease_state` /
  `lease_expires` / `duplicate` / `reason` / `run_id`。报文畸形返回
  **HTTP 422** 且带 `fail_category`；坏 JSON/坏十六进制返回 400——异常不会被
  折叠成成功。
- `GET /api/v1/leases`、`/api/v1/journal`、`/api/v1/events`

快速手工回放：

```bash
curl -s 127.0.0.1:18067/api/v1/version | jq
# 生成一个 DISCOVER（示例，可用 scripts/smoke.sh 中的 Python 组包器）
curl -s -XPOST 127.0.0.1:18067/api/v1/inject \
  -d '{"datagram_hex":"01010600............63825363350101ff"}' | jq
```

## 测试与可诊断性

- **独立参考实现**：`testfixture/wirekit` 自行按 RFC 字节布局组/解包，不 import
  被测代码；测试用它构造输入、并用它校验服务器回复，避免“自测自”。
- **真实环回 UDP**：合成客户端在 `127.0.0.1` 高端口上完成完整收发，覆盖
  并发争抢、旧事务重放、客户端重启（新 socket + INIT-REBOOT）、过期释放、
  畸形报文丢弃、地址池耗尽、持久化重启恢复。
- 测试断言**具体结果与失败类别**（具体动作、reason 常量、地址归属、租约状态、
  选项值、剩余租期秒数），不是“接口能调用”。
- 日志每行 JSON 带 `run_id`、`test`、`step/phase`、`xid`、`chaddr`、
  `client`、输入、期望值/实际值/判定依据；未知或异常状态记为 FAIL/UNKNOWN，
  不存在“统一返回成功”。UDP 传输日志带 `run_id/seq/peer/xid/recv_type`。

## 明确不做的事（子集边界）
- 不实现 DECLINE/INFORM（线模型直接归类 `unsupported_message_type`）。
- 不实现 DHCPv6、BOOTP 中继（giaddr）处理、广播投递（实验室一律单播回 peer；
  广播标志仍被解析、记录并保留在报文头中）。
- 不做权限/租约文件兼容 ISC/KEA，不提供动态 DNS、不做选项 55 按需裁剪
  （固定返回掩码/网关/DNS/租期）。
- 监听策略是安全闸：非环回或特权端口须显式 `allow_non_loopback`，且程序会大声告警。

## 依赖与可复现
- Go 1.23；唯一外部依赖 `modernc.org/sqlite v1.34.5`（纯 Go，已 `vendor/`）。
- `go.sum` 固定全部校验和；无网络时用 `-mod=vendor`（或 `GOPROXY=off`）。
- 时间在测试中通过可注入 `Clock` 控制，过期/续租判定确定性，不依赖 sleep 碰运气。
```
