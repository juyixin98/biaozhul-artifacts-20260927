# igmpv2timer — 离线 IGMPv2 成员/组查询计时服务

一个**离线、确定性**的 IGMPv2（[RFC 2236](https://www.rfc-editor.org/rfc/rfc2236)，
LMQ 过程与 [RFC 3376](https://www.rfc-editor.org/rfc/rfc3376) §6 对齐）成员关系与
组查询计时模拟器。输入是合成的 **报告（Report）/离开（Leave）/查询（Query）**
事件流，输出是逐事件判定（接受/抑制/陈旧/拒绝/超时/丢弃/无法判定）、转发表
保留区间与查询世代。

> **范围声明（重要）**：本项目只模拟**面向主机侧的成员关系定时器**——报告抑制、
> 组成员间隔（Group Membership Interval）、最后成员查询（Last-Member-Query）。
> 它**不是组播路由协议的完整实现**：没有 querier 选举、没有 PIM、没有真实报文
> 收发（无 socket I/O）。所有时间来自注入时钟，所有参与者均为本地合成夹具。

## 它回答的具体问题

针对给定合成场景，测试断言具体结果而不只是"接口可调用"：

1. **两成员交错报告**：每轮查询后谁的延迟报告先到、谁被抑制？组成员截止时刻被
   精确刷新到哪个值？转发项是否连续保留？
2. **查询丢失**：一个 General Query 在传输中丢失后，转发表是否恰好在最后一次
   报告后的一个 GMI 边界删除（549 在场、550 缺席）？事后迟到的、应答**旧查询
   世代**的报告是否被判为 `STALE` 而不会重建已删除的组？
3. **离开后重新加入**：一个成员离开、另一成员仍在时，组是否保留且不发组查询？
   最后成员离开触发 LMQ 后，在查询序列中途重入能否取消删除（世代防护）？
4. **边界超时**：转发项的保留区间 `[start,end]` 是否为精确数值（如
   `100..2300`），删除发生在 `leave + LMQC×LMQI` 的精确边界。

## 模块划分（每个模块有真实职责）

```
cmd/igmpv2timer/        CLI 入口：replay / serve / scenarios
cmd/dbcheck/            SQLite 日志检查小工具（可选）
internal/config/        配置解析与校验（RFC 定时器常量、接口）
internal/clock/         注入式单调虚拟时钟（只能前进，毫秒）
internal/model/         领域/线类型、判定枚举、地址校验与脱敏
internal/netmodel/      合成网络模型：LAN 与响应式主机（主机侧抑制行为）
internal/core/          ★ 被测核心：IGMPv2 成员/查询计时状态机
internal/store/         SQLite 事件日志与派生状态（modernc 纯 Go 驱动，无 CGO）
internal/engine/        离线回放引擎：驱动时钟、查询丢失、抑制、断言、日志重建
internal/server/        标准库 net/http 交互接口（同一状态机，无墙钟）
testdata/scenarios/     三个场景夹具（输入，JSON）
testind/oracle/         ★ 独立参考实现（不 import 任何被测包）
testind/scenario/       ★ 独立测试：用 oracle 交叉校验核心的具体输出
configs/                示例配置
scripts/                示例调用脚本
```

**参考答案不是由被测核心生成的**：`testind/oracle` 是一份独立的、用自己的本地
类型重新解析夹具 JSON、独立重算每条判定/截止时刻/世代/保留区间的参考实现。
`testind/scenario` 同时运行核心与 oracle 并双向比对。

## 快速开始

需要 Go 1.23+。依赖（纯 Go SQLite）已在 `go.mod` 锁定：

```bash
go build ./...
go test ./...                 # 全部单元测试 + 独立场景测试（-race 亦通过）
```

### 离线回放三个场景

```bash
go run ./cmd/igmpv2timer replay testdata/scenarios/s1_interleaved_reports.json
go run ./cmd/igmpv2timer replay testdata/scenarios/s2_query_loss_stale.json
go run ./cmd/igmpv2timer replay testdata/scenarios/s3_leave_rejoin.json
```

输出包含：
- 逐事件轨迹 `t / request_id / 报文 / 接口 / 组 / 成员 / VERDICT / reason`，
  以及关键状态（membership deadline、成员集合、查询世代）；
- 每条断言的 `PASS` 或 `FAIL <失败类别>`；
- `JOURNAL REBUILD OK`：仅用 SQLite 中记录的输入事件，用一个全新核心重建
  成员状态，并与现场最终状态/区间逐字段比对一致。

### HTTP 交互模式（同一注入时钟状态机）

```bash
go run ./cmd/igmpv2timer serve -config configs/config.json
# 或：go run ./cmd/igmpv2timer -config configs/config.json serve
```

端到端示例见 `scripts/http_example.sh`。端点：

| 方法/路径 | 作用 |
|---|---|
| `GET /healthz` | 健康、当前虚拟时间、范围声明 |
| `POST /events` | 注入一个 `report` / `leave` / `general_query`（body 带 `at_ms`） |
| `POST /tick` | 把注入时钟推进到 `to_ms`，触发到期定时器 |
| `GET /state` | 当前成员/转发表快照 |
| `GET /diags` | SQLite 中的判定记录 |
| `GET /intervals` | 转发项保留区间 |

HTTP 状态码映射：接受 `200`；无法判定 `202`；陈旧轮次 `409 Conflict`；
拒绝（坏地址、未知接口、向后拨钟等）`422`；请求体错误 `400`。所有响应都带
`X-Request-ID`（可由请求头 `X-Request-ID` 提供关联 id）。

## 判定（Verdict）与失败类别

| Verdict | 含义 |
|---|---|
| `ACCEPTED` | 报告创建/刷新了成员关系，或离开启动了 LMQ |
| `SUPPRESSED` | 该成员本轮延迟报告被同组更早的报告抑制（IGMPv2 §3） |
| `STALE` | 应答的是已被新世代取代的旧查询轮；不得覆盖新轮/重建已删组 |
| `REJECTED` | 非法输入（坏组地址、坏源地址、对不存在的组离开等） |
| `TIMEOUT` | GMI 内无报告，或 LMQ 序列结束无应答，删除转发项 |
| `DROPPED` | 路由侧已发出查询，但夹具的合成传输层把它丢弃（查询丢失） |
| `UNDECIDABLE` | 引用了本回放中不存在的查询世代，无法判定 |

断言失败时给出稳定类别：`FAIL_GROUP_EXPECTED_PRESENT`、
`FAIL_GROUP_EXPECTED_ABSENT`、`FAIL_DIAGNOSTIC_NOT_FOUND`、
`FAIL_DIAGNOSTIC_AMBIGUOUS`、`FAIL_PACKET_COUNT_MISMATCH`、
`FAIL_RETENTION_INTERVAL_MISMATCH`、`FAIL_TIMER_GENERATION_MISMATCH`。

## 定时器语义（夹具可覆盖）

| 参数 | RFC 默认 | 夹具字段 |
|---|---|---|
| Query Interval (QI) | 125000 ms | `query_interval_ms` |
| Query Response Interval (QRI) | 10000 ms | `query_response_interval_ms` |
| Group Membership Interval (GMI) | QI+QRI | `group_membership_interval_ms` |
| Last Member Query Interval (LMQI) | 1000 ms | `last_member_query_interval_ms` |
| Last Member Query Count (LMQC) | 2 | `last_member_query_count` |

- 报告把 `(iface,group)` 的成员截止时刻刷新为 `now + GMI`；到期删除转发项。
- 最后一个成员离开：**立即**发第一个组查询，再以 LMQI 间隔重传 LMQC−1 次；
  在 `leave + LMQC×LMQI` 前若有报告则取消删除（新成员世代），否则删除。
- General Query 携带**每接口单调递增的世代号**；主机响应报告自己应答的世代。
  组已删除且应答世代 < 当前世代时判 `STALE`。
- 多个成员中任一个离开而仍有成员时，**绝不**启动 LMQ、**绝不**删组。

## 诊断与脱敏

每条判定都带 `request_id`（脚本 `request_id` 或自动生成的 `evt-N`/`timer-N`）、
虚拟时间、接口、组、成员、报文类型、判定、原因、成员截止时刻、活动/应答世代号和
当前成员集合——足以说明**为何接受、拒绝或无法判定**。

主机源地址是唯一可能可标识的信息：日志/轨迹中对源 IPv4 的末段八位组脱敏
（`192.0.2.11 → 192.0.2.x`）；组播组地址（如 `239.1.2.3`）不是主机标识，照常
打印。成员一律使用合成名字（`hostA`/`hostB`）。

## 数据与依赖

- 所有主机、地址、事件均为本地合成夹具（RFC 5737 文档网段 192.0.2.0/24），
  无需任何生产账号或真实业务数据。
- 唯一外部依赖是 `modernc.org/sqlite`（纯 Go、无 CGO），版本在 `go.mod`/
  `go.sum` 锁定；SQLite 文件路径可配置，测试用 `:memory:`。

## 已知限制

- 只建模单个场景组的响应式主机（同一 LAN 可有多成员）；不做跨组的完整 MDB。
- 不实现 IGMPv1/v3、querier 选举、S 标志、SSM、PIM 与真实收发包。
- 主机每轮报告延迟在夹具中以确定值（或逐轮序列）钉死，以换取可精确断言；
  真实实现中该延迟是 `[0,MaxRespTime]` 内的随机值。
- HTTP 模式为单实例、无鉴权的本地交互式模拟器，不应用于暴露网络。
