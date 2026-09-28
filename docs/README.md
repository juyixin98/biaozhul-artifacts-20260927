# ribd — 版本化 RIB 后端

一个从零实现的 IPv4/IPv6 路由信息库（Routing Information Base）后端，核心约束：

- **前缀规范化**：所有前缀在入口处掩码到网络地址并按规范文本输出（IPv6 遵循 RFC 5952，
  拒绝 IPv4 八位组前导零、IPv4-mapped IPv6、zone 地址）。
- **地址族隔离**：IPv4 与 IPv6 各持有独立的压缩前缀树，查表与递归解析永不跨族。
- **最长前缀优先，再比较管理距离**：目标地址先做最长前缀匹配（LPM）；只有同前缀的候选
  之间才比较管理距离（AD）→ 度量值（metric）→ 安装顺序（sequence）。短前缀即使 AD 更优
  也不会盖过长前缀。

另外明确实现的行为：

- 同前缀候选固定选路策略：`AD 小者胜 → metric 小者胜 → 先安装者胜`。
- 递归下一跳：解析直连接口才终止；检测自环与多跳环；递归深度有配置上限（默认 8）。
- 批量替换原子可见：一个批次先完整校验，再在一个 SQLite 事务中持久化，最后整体切换到新表
  版本；失败时在线版本完全不动，旧快照读者不受影响（持久化路径复制）。

## 模块划分

| 包 | 真实职责 |
| --- | --- |
| `internal/netmodel` | 地址族、规范化前缀、地址、路由与下一跳模型及入口校验 |
| `internal/trie` | 泛型压缩二进制前缀树（Patricia/radix），持久化复制，返回命中链 |
| `internal/rib` | 每族一棵树、版本化快照、原子批次、选路排序、递归解析与环/深度判定 |
| `internal/config` | JSON 配置严格解析（拒绝未知键）、默认值、带字段路径的校验 |
| `internal/store` | SQLite 持久化：单事务批次、事件流、版本快照表、乐观版本检查 |
| `internal/replay` | 从事件流重建任意版本，并与持久化快照独立核对（不依赖在写路径） |
| `internal/server` | 本地 HTTP 接口：批次、查询、事件、回放、版本 |
| `internal/diag` | 请求标识、结构化 JSON 诊断记录、敏感字段脱敏 |
| `cmd/ribd` | 装配与冷启动：打开存储 → 回放事件或安装引导路由 → 启服务 |

## 构建与测试（离线）

依赖已锁定（纯 Go 的 `modernc.org/sqlite v1.34.4`，无需 cgo）。所有依赖都在本机模块缓存中，
可用 `GOPROXY=off` 离线构建：

```bash
go build ./...
GOPROXY=off go test -race -count=1 ./...
```

测试组织（代码与测试目录分离，外部测试包只使用公开 API）：

- `internal/trie/trie_test.go`：4-bit 键空间穷举插入/删除/命中链、真实 IPv4 形态。
- `internal/netmodel|config|diag/*_test.go`：规范化、严格配置解析、脱敏。
- `internal/rib/rib_test.go`：批次原子性、旧快照稳定、族隔离、AD/metric/sequence 选路、删除语义。
- `test/golden_test.go`：**独立 golden 套件**，对照 `testdata/golden/golden.json`
  中人工逐项编写的参考答案，断言具体状态、失败类别、匹配链与递归链。
- `test/store_replay_test.go`：SQLite 事务、事件重放、任意版本重建、重启 Adopt、快照核对。
- `test/server_test.go`：HTTP 端到端（含 request id 回传、dry-run、脱敏输出）。

golden 参考答案不是被测核心生成的：文件带人工编写标记并有测试守护
（`TestGoldenIsHandAuthored`），任何“按实现更新参考”的行为都会被发现。

## 运行

```bash
# 内存模式（不落盘）
go run ./cmd/ribd -config testdata/config/memory.json

# SQLite 持久化模式（首次启动安装 routes 引导批次，之后从事件流恢复）
go run ./cmd/ribd -config testdata/config/example.json
```

## HTTP 接口

| 方法/路径 | 作用 |
| --- | --- |
| `POST /v1/batches` | 原子提交一个 upsert/delete 批次；`dry_run:true` 只校验 |
| `GET  /v1/lookup?target=<ip>` | LPM + 递归解析，返回完整匹配链/递归链/判定原因 |
| `GET  /v1/routes` | 列出当前版本全部路由（meta 敏感值脱敏） |
| `GET  /v1/version` | 当前表版本与路由数 |
| `GET  /v1/events?to=N` | 持久化事件批次（回放数据源） |
| `POST /v1/replay` | 从事件流重建：`{"to_version":N,"verify":true}` |
| `GET  /healthz` | 存活检查 |

所有响应回传 `X-Request-ID`（可由请求头指定，否则服务端生成）。

### 判定状态与失败类别

`GET /v1/lookup` 的 `status`：

- `resolved`：最终到达直连下一跳（出接口）。
- `indeterminate`：`no_route`（无覆盖前缀）或 `unresolved`（递归下一跳无任何路由覆盖）。
  无法判定，不是拒绝。
- `rejected`：`loop`（自环/多跳环）、`depth_exceeded`（超过 `max_depth`）、
  `cross_family`（族穿越，入口校验也会拒绝）、`bad_query`（目标地址非法）。

每条结果带 `reason`，包含表版本、在哪一步停止等关键状态。服务端 stderr 同步输出结构化
JSON 诊断行；meta 中键名含 `token/password/secret/key/community/private-key` 等片段的值
一律打印为 `[REDACTED]`。

## 示例调用

见 `examples/calls.sh`。简版：

```bash
curl -s -XPOST localhost:8080/v1/batches -d '{
  "changes":[
    {"kind":"upsert","route":{"id":"d","prefix":"0.0.0.0/0","admin_distance":10,
     "next_hop":{"addr":"203.0.113.1"}}},
    {"kind":"upsert","route":{"id":"p","prefix":"203.0.113.0/24","admin_distance":0,
     "next_hop":{"interface":"eth1"}}}
  ]}'

curl -s 'localhost:8080/v1/lookup?target=192.0.2.5'
# status=resolved, chosen_route=d, egress=eth1, resolve_chain 两步

curl -s -XPOST localhost:8080/v1/replay -d '{"verify":true}'
```

## 关键设计说明

- **为什么 AD 不参与 LPM**：`match()` 返回的命中链按前缀长度降序，仅首个元素 `chosen=true`；
  更短前缀作为影子候选保留在链中，便于诊断“为什么没选它”。AD/metric/sequence 只在同前缀
  的候选集合内部排序（`sortEntries`）。
- **为什么批次能同版本可见**：`trie` 是持久化结构（路径复制），`Snapshot.build` 在副本上
  校验整批，`Persister.Persist` 单事务落库后才 `atomic.Pointer.Store` 切换；读者始终持有的
  是不可变快照。
- **环检测**：解析链上记录已使用的路由 id（步骤号），下一跳等于当前解析目标即自环，
  路由 id 再次出现即多跳环；两种都返回 `rejected/loop` 并给出涉及的路由与步骤。
- **回放独立性**：`internal/replay` 从空表、禁用持久化重新应用事件流，再与
  `routes_snapshot` 表逐项比对，发现 `missing_in_store / extra_in_store / payload_differs`。

## 已知限制

- 没有真实控制平面（BGP/OSPF/Netlink）；路由只通过配置引导与 HTTP 批次进入。
- 不做 ECMP：同前缀同 AD/metric/seq 之外不做多下一跳等价分担（最终 tie-break 是确定的安装顺序）。
- 递归解析只在同一地址族内进行（这是刻意的隔离约束，不是缺陷）。
- SQLite 写连接串行化（`SetMaxOpenConns(1)` + WAL），面向本地单机/夹具规模，未做分片或集群。
- HTTP 服务无鉴权与 TLS，按需求定位为本地接口；请勿直接暴露到不可信网络。
