# 离线 PCAP IPv4 分片重组后端

离线（不发送任何网络报文）的 IPv4 分片重组实现：从本地合成 PCAP 夹具或 JSON
注入读取 IPv4 分片，按 RFC 791 语义重组，并把状态与审计日志持久化到 SQLite；
同时提供仅监听 localhost 的 HTTP 回放接口与命令行工具。

> 本工程只做 **IP 层分片重组**：按 `(源地址, 目的地址, 协议, IP identification)`
> 分组，重组对象是 IPv4 payload。它不维护任何 TCP 连接/序列号状态，也不做
> TCP 流重组；未分片的 IP 包（如普通 TCP 段）直接计入 `unfragmented` 旁路，
> 不进入重组器。

## 目录与模块关系

```
config/reasm.json           配置样例（超时、长度上限、容量、DB、监听地址）
internal/config/            配置层：JSON 解析、默认值、严格校验（未知字段报错）
internal/ipv4/              网络模型层：IPv4 头解析/序列化（校验和）、偏移字段
internal/pcap/              经典 libpcap 读写器 + 以太网/802.1Q/raw-IP 解封装
internal/reasm/             核心：分组键、超时、重叠整组拒绝、重复片识别、输出判定
internal/store/             状态存储层：SQLite（fragments/datagrams/events 三表）
internal/replay/            回放层：PCAP/JSON -> 重组器 -> 确定性报告 + HTTP API
cmd/genfixture/             独立夹具与 golden 期望生成器（不依赖被测重组核心）
cmd/reasm-cli/              离线回放 CLI，可按 manifest 做退出码判定
cmd/reasm-server/           localhost HTTP 回放服务
testdata/                   已提交的合成 PCAP、golden payload、manifest.json
scripts/verify.sh           一键本地验证
```

依赖方向：`replay -> (reasm, pcap, ipv4, store)`，`reasm` 不依赖存储与网络层，
通过 `Sink` 接口上报状态转移；`store` 实现该接口把状态写入 SQLite。

## 算法契约与假设

### 分组键与超时
- 分组键 `Key{Src, Dst, Proto, ID}`，即源、目的、协议号与 16 位 identification。
- 每组维护显式空闲超时（默认 30s）。PCAP 回放时**时钟由报文捕获时间驱动**
  （每条记录到达先做一次 `Sweep(now)` 再入片），因此超时行为完全确定、可离线复现。
- 超时定义：`now - lastSeen > timeout`（严格大于）。任何属于本组的分片
  （包括完全重复片）都会刷新 `lastSeen`。
- 超时后分组被删除、缓冲行回收，日志记 `expired`；此后同一 identification
  出现的新分片组成**全新、独立**的分组（`timeout_reuse.pcap` 覆盖该场景）。

### 偏移与长度校验
- 线上 `fragment offset` 是 13 位、单位 8 字节；入引擎即换算成字节偏移。
- 除末片（MF=0）外，载荷长度必须是 8 的倍数且非空，否则 `bad-length`。
  末片长度任意（RFC 791 允许），测试 `unit-tail-unaligned-001` 覆盖。
- `offset*8 + len` 不得超过配置上限（默认 65535），否则 `oversize`。

### 重叠策略：整组拒绝；完全重复单独识别
- 新区间 `[off, off+len)` 与任一已缓冲区间相交时：
  - **偏移相同、长度相同、字节完全相同、MF 一致** → `duplicate`（单独计数，
    不写第二行，不视为重叠）；
  - 字节相同但 MF 不同 → `conflicting-last`；
  - 其他任何相交（包括“前缀相同但长度不同”的伪重传）→ `overlap`，
    **整组拒绝**：已缓冲字节立即回收，分组转为“毒化墓碑”，在超时前该键的
    后续分片一律返回 `poisoned-group` 且不落盘。
- 末片不一致（同一键出现两个不同的总长）或非末片越过已知总长 →
  `conflicting-last`，同样整组拒绝。

### 输出条件（不提前输出）
仅当末片已知（总长得确定）**且**缓冲区间无缺口地铺满 `[0, total)` 时，才输出一次
完整 payload。末片先到、中间缺片都只入缓冲不输出；直到超时被回收，绝不产生
半截/垃圾输出。完成后分组及分片行立即删除，完成结果写入 `datagrams` 表。

### 容量保护
`max_datagrams`（并发分组数）与 `max_buffered_bytes`（全局缓冲字节）超限以
`capacity` 类别显式拒绝，错误绝不伪装成成功。

## SQLite 状态模型

| 表 | 内容 |
|---|---|
| `fragments` | 在途分片（payload blob），以 `(run_id,key,offset)` 为主键；分组完成/拒绝/超时即删除——资源回收测试直接断言行数归零 |
| `datagrams` | 完成的重组结果：payload、sha256、分片数、完成时间 |
| `events` | 追加审计日志：`stored/duplicate/completed/rejected/expired`，含 run_id、单调 seq、明细（偏移、长度、判定原因）|

不同回放通过 `run_id` 隔离；HTTP 与 CLI 日志、报告均带 run_id 可关联。

## 依赖版本

- Go `1.23.4`（`go.mod` 声明 `go 1.23`）
- `modernc.org/sqlite v1.34.5`（纯 Go SQLite，**不需要 cgo**；更高版本要求 go1.25）
  传递依赖：`modernc.org/libc v1.55.3`、`modernc.org/mathutil v1.6.0`、
  `modernc.org/memory v1.8.0`、`golang.org/x/sys v0.22.0`
- 其余仅使用 Go 标准库（net/http、net/netip、encoding/json…）

## 本地验证命令

前置：Go 1.23+（无 cgo 要求）。

```bash
# 一键验证（构建/vet/夹具确定性/全部测试/CLI golden）
./scripts/verify.sh

# 或分步执行：
go build ./...
go vet ./...
go test ./... -count=1 -v          # 详细日志，含 run id、go 版本、判定依据

# 重新生成夹具（确定性，manifest 不应产生 diff）
go run ./cmd/genfixture -out testdata

# 单文件回放并按 golden manifest 判定（退出码 0/1）
go run ./cmd/reasm-cli -config config/reasm.json \
  -pcap testdata/timeout_reuse.pcap -run demo-1 -db /tmp/demo.db \
  -expect testdata/manifest.json

# 启动仅监听 127.0.0.1:8080 的回放服务
go run ./cmd/reasm-server -config config/reasm.json
curl -s http://127.0.0.1:8080/healthz
curl -s -X POST 'http://127.0.0.1:8080/v1/replay/pcap?run_id=smoke' \
  --data-binary @testdata/basic.pcap
curl -s 'http://127.0.0.1:8080/v1/runs/smoke/events'
```

### 预期判定方式
- `go test`：全部包 `ok`，无 `FAIL`。测试不是“能调用即通过”：
  - `internal/reasm`：6 个分片的 **全部 720 种到达排列**、50 组带随机种子（日志
    记录 seed 可复现）的乱序+重复、末片先到、缺片不输出、完全重复计数、
    MF 翻转、重叠整组拒绝+墓碑、长度不一致伪重传、冲突末片、越过总长、
    bad-length、oversize、分组/字节容量、超时后 ID 复用——均断言**具体结果与
    失败类别**（`overlap` / `conflicting-last` / `bad-length` / `oversize` /
    `capacity` / `poisoned-group`）和统计计数。
  - `internal/store`：缓冲中行数随状态变化（1→完成/拒绝/超时后 0）、
    datagram payload 与 sha256、事件类别计数、run_id 隔离。
  - `internal/replay`：对 5 个合成 PCAP 做端到端断言，并把输出与
    **由独立生成器在分片之前保存的 golden payload/sha256** 逐字节比对；
    末尾 SQLite 缓冲行必须为 0（资源回收）；未分片包必须旁路。
  - HTTP 测试：成功路径与 400 显式失败（垃圾 PCAP、非法 run_id、坏地址），
    错误响应必为 `{"ok":false,...}`，未知/异常状态不会返回成功。
- `reasm-cli -expect`：逐行打印 `PASS/FAIL` 与判定依据，末尾
  `SUMMARY <fixture>: n/n checks passed`，任一不符退出码为 1。

### 合成夹具（testdata/，参考答案独立生成）
| 文件 | 场景 | 期望 |
|---|---|---|
| `basic.pcap` | 正序、末片先到逆序、乱序+1 个完全重复、外加 1 个未分片包 | 3 个完成（dup 计数 1），1 个旁路 |
| `overlap.pcap` | `[0,64)` 与 `[128,288)` 字节冲突，后续迟到片 | rejected: overlap，其后 poisoned |
| `conflict_last.pcap` | 两个总长不同的末片 | rejected: conflicting-last |
| `badlen.pcap` | MF 片长度 84（非 8 倍数） | rejected: bad-length |
| `timeout_reuse.pcap` | F 残缺、G 缺中间片，30s 后 F 的 ID 被新数据报复用 | F、G 各 expired 一次，随后 F 按**新**载荷完成 |

期望值不是由被测重组代码产生的：`genfixture` 先确定地生成原始 payload
（`mkPayload`）、写 `golden_*.bin`，再用生成器自己的分片逻辑切片；测试和 CLI
只拿重组输出与这些原始字节比对。

## 测试状态

最后一次本地执行：`./scripts/verify.sh` 全部步骤通过；`go test ./...`
在 go1.23.4/linux-amd64 下全部包 `ok`。如某环境未运行某步骤，verify.sh 会
以非零退出码如实标记，不会跳过或把失败计为成功。
