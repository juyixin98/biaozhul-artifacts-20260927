# RIB — IPv4/IPv6 路由表后端

一个用 Go 标准库 + SQLite 实现的路由信息库：

- **前缀规范化**：所有 CIDR 入库即掩码清零、IPv6 零压缩小写（基于 `net/netip`）。
- **地址族隔离**：IPv4 与 IPv6 各有一棵独立前缀树，跨族下一跳在入口即被拒绝；
  IPv6 查询永远不会“掉”到 IPv4 默认路由，反之亦然。
- **最长前缀优先于管理距离**：先做 LPM 取最深覆盖前缀，*然后*才在同前缀候选间
  按管理距离 → metric → 协议固定先后 → 下一跳地址 → ID 决胜。
- **路径压缩前缀树（Patricia trie）**：写时复制（COW），读不持锁；
  默认路由 `/0` 与主机路由 `/32`、`/128` 可共存。
- **递归下一跳解析**：返回完整**匹配链**，检出递归环与深度上限，分类为
  稳定的失败状态（`recursion_loop` / `depth_exceeded` / `nexthop_unresolved` …）。
- **单表版本原子可见**：批量替换先整体校验、建树，再一次换根、只递增一次版本，
  观察者只能看到旧表或完整新表。
- **SQLite 持久化**：`routes` 当前快照 + 只追加的 `events` 变更日志，
  事件与快照在同一事务内提交，可重放并与当前表逐项对照。

## 目录结构

```
cmd/routed/             服务入口（配置→SQLite→恢复/种子→HTTP）
internal/
  netmodel/             网络模型：规范化前缀、地址族、路由与下一跳校验
  trie/                 泛型路径压缩二进制前缀树（COW 快照）
  rib/                  选路核心：LPM、固定策略、递归解析、版本、批量替换
  store/                SQLite 持久化（快照表 + 事件日志，单事务）
  replay/               事件回放与当前状态一致性对照
  config/               JSON 配置 + RIB_ 环境变量覆盖 + 校验
  diag/                 请求标识、结构化诊断、地址脱敏
  api/                  HTTP 接口（net/http ServeMux，无第三方框架）
  acceptance/           独立验收：手写 golden + 独立朴素参考实现 + 随机对拍
configs/                示例配置与种子夹具
testdata/golden/        手工推导的验收参考答案（非被测代码生成）
scripts/demo.sh         端到端示例调用
```

## 快速开始

需要 Go 1.23+。SQLite 驱动使用纯 Go 的 `modernc.org/sqlite`，**不需要 cgo**。

```bash
go build -o bin/routed ./cmd/routed

# 用示例种子启动（文件数据库，进程重启后自动从快照恢复）
mkdir -p data
RIB_SQLITE_DSN='file:data/rib.db?cache=shared' ./bin/routed -seed configs/seed.example.json

# 或使用配置文件
./bin/routed -config configs/config.example.json
```

配置也可用环境变量覆盖：`RIB_LISTEN_ADDR`、`RIB_SQLITE_DSN`、`RIB_MAX_DEPTH`、
`RIB_REDACT_DIAG`、`RIB_LOG_LEVEL`。

跑端到端演示（构建二进制后）：

```bash
./scripts/demo.sh
```

## HTTP 接口

所有响应统一为 `{request_id, success, version, data|error}`。
可用请求头 `X-Request-ID` 关联日志与响应；不提供时自动生成 `req-<hex>`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康与当前表版本 |
| GET | `/v1/routes?family=ipv4\|ipv6` | 列出当前路由 |
| POST | `/v1/routes` | 插入/覆盖同 (前缀,ID) 候选 |
| DELETE | `/v1/routes?prefix=…&id=…` | 删除候选（404 `not_found`） |
| POST | `/v1/routes/replace` | 整表批量替换（单版本原子可见） |
| GET | `/v1/lookup?target=<addr>` | LPM + 递归解析，返回匹配链 |
| GET | `/v1/events` | 读取变更事件日志 |
| POST | `/v1/replay` | 重放事件并与当前表对照（不一致返回 409） |

路由写入示例：

```bash
curl -sS -X POST localhost:8080/v1/routes -H 'Content-Type: application/json' \
  --data '{
    "id":"host-a","prefix":"10.10.10.1/32",
    "admin_distance":200,"metric":0,"protocol":"static",
    "nexthop":{"kind":"address","address":"203.0.113.254"}
  }'

curl -sS 'localhost:8080/v1/lookup?target=10.10.10.1'
```

查询返回的 `data.status` 取值：

- `forwarded` —— 解析到直连，`egress` 为出接口；
- `blackhole` / `unreachable` —— 命中终止型路由；
- `no_route` —— 无任何覆盖前缀（含默认路由）；
- `nexthop_unresolved` —— 递归下一跳没有覆盖路由；
- `recursion_loop` —— 再次进入已访问的路由条目，`chain` 末跳为重入项；
- `depth_exceeded` —— 无环节点链跳数超过 `max_recursion_depth`。

## 选路语义（关键约束）

1. 解析在**某一族内**进行；LPM 在该族树中取最深覆盖前缀。
   因此 `10.10.10.1/32 (AD 200)` 永远胜过 `0.0.0.0/0 (AD 1)`。
2. 同前缀多候选按固定全序决胜：
   `admin_distance` ↑、`metric` ↑、协议序（connected<static<ospf<isis<bgp）、
   下一跳地址 ↑、ID ↑。协议序之外的未知协议按名确定序，保证结果可复现。
3. 选中递归下一跳后，对下一跳地址在**同族**重新做 LPM，直到终局
   （直连/黑洞/unreachable）或失败；每进入一条路由记录一跳匹配链。
4. 环检测以 `前缀#ID` 为键，先判环再计数深度；深度上限默认 16，可配置。

## 持久化与回放

- 每个写请求：内存更新 + SQLite 单事务（`routes` upsert/delete + `events` 追加）。
  落盘失败时内存做补偿回滚（见 `internal/api/server.go` 注释；局限见下）。
- `POST /v1/replay` 从空表重放全部事件，与运行中表做路由集合 diff；
  完全一致时 `consistent:true`，否则 HTTP 409 并列出差异。
- 启动时默认从 `routes` 快照恢复；`-seed` 在恢复后做一次批量替换
  （快照清空重写 + 单条 `replace_all` 事件）。

## 诊断与脱敏

- 每次接受/拒绝/无法判定都带原因：响应内 `data.diagnostics` 或
  `error.diag`，以及 stderr 的结构化访问日志（含 `request_id`、事件序号）。
- `redact_diagnostics` 默认开启：IPv4 仅保留前两个八位组（`203.0.x.x`），
  IPv6 仅保留首组（`2001::xxxx`）。目标地址本身在 `data.target` 中仍完整返回，
  脱敏只作用于日志/诊断文本。

## 测试

```bash
go test ./...                 # 全量
go test -race ./...           # 竞态
go test ./internal/acceptance -v   # golden 逐案例 + 独立参考对拍
```

测试如何独立于被测实现：

- `internal/trie`：4000 轮/地址宽度的随机增删查，对拍一个**线性朴素模型**；
  另含压缩回收、COW 快照、IPv6 全宽等结构断言。
- `internal/acceptance`：
  - `testdata/golden/cases.json` 是**手工推导**的参考答案，逐字段断言
    状态、完整匹配链（ID 与规范化前缀）、深度、出接口；
  - `reference.go` 是独立的线性 LPM 参考解析器（不使用 `trie`/`rib` 选路代码），
    随机表上与被测核心对拍；
  - 非法输入断言**具体错误类别**（跨族、越界掩码、缺下一跳……），
    而不是“接口能调用”。
- `internal/store`、`internal/api`：内存 SQLite 与 `httptest` 端到端，
  含重启后快照恢复、非法批量整体拒绝、回放一致性。

## 剩余限制

- 写路径采用“内存先改、失败补偿回滚”，未把 RIB 更新与 SQLite 事务做成
  单一可串行化事务；进程在二者之间崩溃的极小窗口内，可能以事件日志重放为准修复。
  生产化方向是让 RIB 提交成为事务回调。
- 不支持等价多路径（ECMP）/多下一跳；同前缀多候选只选一条确定胜者。
- 不支持路由协议邻居、重分发、ACL/策略路由；`protocol` 仅参与决胜排序。
- 事件日志只追加不压缩，`GET /v1/events` 一次读取，受 `replay_limit` 约束，
  面向夹具/中小规模，未做分页游标与保留期管理。
- HTTP 未内置鉴权与 TLS，预期仅监听本地回环（默认 `127.0.0.1:8080`），
  由前置代理负责认证与传输安全。
- IPv6 区域地址（zone）不参与路由键；目标地址含 zone 时按去 zone 语义处理。
