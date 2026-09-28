# 场景夹具格式

每个 `*.json` 是一次离线回放脚本，由 `internal/engine` 执行、由
`testind/oracle` 独立重算。时间单位均为**注入虚拟时钟的毫秒**。

## 顶层字段

| 字段 | 说明 |
|---|---|
| `name` / `summary` | 场景名与说明 |
| `iface` / `group` | 本场景聚焦的（接口, 组） |
| `timing_override` | 覆盖配置中的 RFC 定时器（可只给部分字段） |
| `members[]` | 合成响应式主机 |
| `events[]` | 按 `at_ms` 排序的时间线动作 |
| `drop_rules[]` | 合成传输层的查询丢弃规则（模拟查询丢失） |
| `until_ms` | 回放终点；最终在此刻做一次定时器扫描 |
| `assertions[]` | 具名断言 |

## members[]

- `name`、`addr`（必须是合法单播 IPv4；用 192.0.2.0/24 文档网段）
- `general_delay_ms`：收到 General Query 后的报告延迟（≤ QRI）
- `general_delays_ms[]`：**逐轮**钉死延迟（模拟每轮重新随机），如
  `[40,20,40]` 表示第 1/2/3 轮分别延迟 40/20/40ms；超出后取最后一个
- `lmq_delay_ms`：收到组查询后的报告延迟（≤ LMQI）

## events[].kind

| kind | 作用 |
|---|---|
| `report` | 主机主动发送成员报告（创建/刷新；可带 `ref_general_n` 应答第 N 个**已投递** General Query，或 `response_to_gen` 指定世代） |
| `leave` | 主机发送 Leave（多成员时仅移除该成员；最后成员触发 LMQ） |
| `force_query` | 脚本在该时刻注入一个 General Query（`"deliver": false` 强制丢失） |
| `checkpoint` | 不改状态，仅作为某时刻的断言锚点 |
| `advance` | 空动作（时钟由时间线统一推进） |

## drop_rules[]

`{packet, iface?, group?, from_ms, count}`：从 `from_ms` 起，对匹配的
`QUERY_GENERAL`/`QUERY_GROUP` 丢弃前 `count` 个（`count:0` 表示无限）。
被丢弃的查询仍会**发出并推进世代**，只是不到达主机，对应判定 `DROPPED`。

## assertions[].check

| check | 关键字段 |
|---|---|
| `present_at` / `absent_at` | `at_ms`, `iface_p`, `group_p` |
| `diag_matches` | `target{at_ms,member,verdict,reason,gen_active,gen_applied,deadline_ms,members,...}` |
| `count_packets` | `packet`（或 `verdict`）、可选 `at_ms_count` 上界、`want` |
| `interval_equals` | `iface_p`,`group_p`,`start_ms`,`end_ms`（`end_ms=0` 表示仍开放） |
| `generation_guard` | `target` 命中一条 `gen_active != gen_applied` 的判定 |

## 三个内置场景

- `s1_interleaved_reports.json` — 两成员逐轮交错报告与抑制、连续保留区间。
- `s2_query_loss_stale.json` — General Query 丢失、GMI 边界超时、迟到旧轮
  报告判 `STALE` 不重建、精确保留区间 `100..550`。
- `s3_leave_rejoin.json` — 单成员离开不删组、LMQ 立即首查、序列中途重入
  取消后续查询、第二次离开在 `leave+LMQC×LMQI` 精确删除、区间 `100..2300`。
