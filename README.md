# igmpq — 离线 IGMPv2 成员与组查询计时服务

一个**离线、确定性**的 IGMPv2 查询器侧（querier-side）成员关系与查询计时
回放服务。输入是合成的报告（report）、离开（leave）、查询（query）事件流，
输出是每个 (接口, 组) 的转发表保留区间、定时器世代（generation）轨迹、
拒绝事件及其失败类别。

**范围声明**：这不是完整的组播路由协议实现。没有真实报文、没有 PIM/DR
选举、没有 IGMPv3 源过滤；它只建模 RFC 2236 查询器侧的成员状态与计时
行为，用于离线回放与验证。

## 构建与测试

```sh
go build ./...     # 需要 Go 1.23+
go test ./...      # 全部单元测试 + 黄金场景测试
go vet ./...
```

唯一外部依赖是 `modernc.org/sqlite`（纯 Go SQLite，见 `go.mod`/`go.sum`
锁定的版本）。其余全部使用标准库。

## 快速开始

一次性回放一个场景文件（结果打印到 stdout）：

```sh
go run ./cmd/igmpd replay -scenario testdata/scenarios/s1_two_members.json
```

启动 HTTP 服务并回放：

```sh
go run ./cmd/igmpd serve -addr 127.0.0.1:8080 -db file:igmpq.db

curl -s -X POST http://127.0.0.1:8080/v1/replay \
  -d @testdata/scenarios/s5_stale_generation.json
curl -s http://127.0.0.1:8080/v1/runs/1     # 取回已持久化的运行结果
curl -s http://127.0.0.1:8080/v1/runs       # 列出全部运行
```

## 模块划分

| 模块 | 职责 |
|---|---|
| `internal/config` | 配置解析、RFC 2236 默认值、校验；导出派生量 GMI/LMQT |
| `internal/simclock` | 注入式时钟（Manual）。核心引擎从不读墙钟时间 |
| `internal/model` | 合成网络事件模型（report/leave/query/suppressed_report） |
| `internal/engine` | 核心状态机：成员集、组定时器、世代、最后成员查询阶段 |
| `internal/store` | SQLite 持久化：运行结果 JSON + 规范化的事件/变迁/区间表 |
| `internal/replay` | 场景装载与执行，产出完整可观测结果 |
| `internal/httpapi` | HTTP 回放接口、请求 ID、访问日志（成员 IP 脱敏） |
| `internal/cats` | 稳定的失败类别集合（跨模块契约） |
| `internal/redact` | 日志脱敏（IP 掩码） |
| `cmd/igmpd` | CLI：`serve` / `replay` 子命令 |

## 计时语义（引擎契约）

配置参数沿用 RFC 2236 命名，缺省值即 RFC 缺省（QI=125s, QRI=10s, RV=2,
LMQI=1s, LMQC=RV）。派生量：

```
GMI  = RV*QI + QRI     组成员关系间隔（最后一次报告后组保留多久）
LMQT = LMQI * LMQC     最后成员查询总时长
```

- **report**：把组定时器刷到 `now + GMI`。携带显式 `gen`（应答的查询轮次）
  的报告若早于该组当前轮次，视为**旧轮响应**：记入审计
  （`stale_report_ignored`）但不改变任何状态——旧轮响应不能覆盖新轮，
  也不能复活已过期的组。省略 `gen` 表示属于当前轮（如 unsolicited join）。
- **leave**：移除该成员。若仍有其他成员，组与定时器原样保留——**单个
  离开不会删除多成员组**。最后成员离开时进入最后成员阶段，截止期
  `now + LMQT`。
- **最后成员阶段内的 report**：挽救该组，定时器重置为完整 GMI
  （`rescued_last_member`）。
- **general/group query**：推进接口查询世代；组记录当前轮次。查询本身
  不改变定时器（查询丢失时组活到 `上次刷新 + GMI`）。
- **suppressed_report**：主机侧报告抑制（RFC 2236 §3）的显式声明，
  仅审计，查询器不可观测，绝不影响定时器与成员状态。
- **到期为闭区间**：截止期为 T 的组在时刻 T 已不存在；T 时刻到达的
  报告会开启一段**新的**转发区间。

## 事件与场景格式

```json
{
  "name": "demo",
  "config": {"interfaces": ["eth0"], "query_interval_sec": 100, "...": "..."},
  "events": [
    {"time_ms": 0,     "type": "report",        "iface": "eth0", "group": "239.1.1.1", "member": "10.0.0.1"},
    {"time_ms": 20000, "type": "general_query", "iface": "eth0"},
    {"time_ms": 25000, "type": "report",        "iface": "eth0", "group": "239.1.1.1", "member": "10.0.0.1", "gen": 1},
    {"time_ms": 40000, "type": "leave",         "iface": "eth0", "group": "239.1.1.1", "member": "10.0.0.1"}
  ],
  "run_until": 300000
}
```

事件类型：`report`、`leave`、`general_query`、`group_query`、
`suppressed_report`。`time_ms` 必须非递减；`run_until` 让末尾的定时器
到期得以发生。

## 输出与诊断

回放结果包含：`transitions`（带序号/时间/类型/世代/原因/关键状态的
审计轨迹）、`rejections`（事件序号 + 稳定失败类别 + 原因）、
`intervals`（每个 `iface/group` 的转发表保留区间 `[start,end)`，
`end=null` 表示仍存活）、`final_groups`（末态成员、到期时间、轮次、
抑制计数）。

失败类别（`internal/cats`）：`invalid_config`、`invalid_scenario`、
`bad_event`、`out_of_order`、`unknown_interface`、`invalid_group`、
`invalid_member`、`unknown_member`、`unknown_group`、
`future_generation`、`run_not_found`、`bad_request`、`internal`。

每次 HTTP 请求带 `X-Request-Id`，访问日志与错误日志都含该 ID、类别与
关键状态；日志中的成员 IP 一律脱敏（`10.0.0.x`）。完整数据只保存在
本地 SQLite 与 API 响应中（本工具面向 loopback 本地使用）。

## 测试组织

- `internal/engine/engine_test.go` — 直接构造的状态机测试：两成员交错
  报告+逐个离开、查询丢失、离开后重新加入、边界超时（恰好到期 vs 早
  1ms）、旧轮响应不覆盖新轮/不复活、拒绝类别。期望值全部是字面量。
- `internal/replay/replay_test.go` — 黄金场景测试：运行
  `testdata/scenarios/s*.json` 并逐字段比对夹具内**手工推导**的
  `expect` 块（推导过程见 `testdata/scenarios/README.md`，不由被测
  代码生成）。
- `internal/config`、`internal/store`、`internal/httpapi`、
  `internal/simclock`、`internal/redact` 各有独立测试，断言具体结果与
  失败类别（如 422/`invalid_config`、404/`run_not_found`）。

## 已知限制

- 仅建模 IGMPv2 查询器侧：无 IGMPv1 兼容、无 IGMPv3/SSM、无源列表。
- 最后成员阶段建模为聚合截止期（LMQI×LMQC），不逐条模拟每次组特定
  查询重发。
- 报告抑制以显式合成事件声明；引擎不模拟主机侧的随机响应定时器。
- 时间为离散事件驱动的注入时钟，非实时；事件时间必须非递减。
- 单进程、单接口命名空间；SQLite 单写者。HTTP 无认证，仅供本地
  loopback 使用。
