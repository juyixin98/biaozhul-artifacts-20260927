# 场景期望值的手工推导（参考答案）

本目录每个 `s*.json` 的 `expect` 块都是**手工推导**的参考答案，不由被测引擎生成。
`internal/replay/replay_test.go` 把引擎输出与这些字面值逐字段比较。

所有场景共用配置 A（s1–s6）：

```
query_interval_sec (QI)            = 100
query_response_interval_sec (QRI)  = 10
robustness_variable (RV)           = 2
last_member_query_interval (LMQI)  = 5
last_member_query_count (LMQC)     = 3
```

按 RFC 2236 §4 的公式推导：

```
GMI  = RV*QI + QRI = 2*100 + 10 = 210 s   = 210000 ms   （组成员关系间隔）
LMQT = LMQI * LMQC = 5*3        = 15 s    = 15000 ms    （最后成员查询总时长）
```

计时规则（引擎语义，见 `internal/engine` 包注释）：报告把组定时器刷到
`now + GMI`；最后成员离开时截止期改为 `now + LMQT`；**到期是闭区间**——
截止期为 T 的组在时刻 T 已不存在。查询只推进世代（generation），不动定时器。

## s1_two_members —— 两个成员交错报告、抑制、逐个离开

| t(ms) | 事件 | 推导 |
|---|---|---|
| 0 | m1 report | 建组，expiry = 0+210000 |
| 10000 | m2 report | 刷新，expiry = 220000 |
| 20000 | general query | 世代 → 1 |
| 25000 | m1 report gen=1 | 刷新，expiry = 235000 |
| 26000 | m2 suppressed_report | 仅审计，**expiry 不变**（抑制不刷新定时器） |
| 40000 | m1 leave | 还剩 m2 → 组保留，expiry 不变 |
| 50000 | m2 leave | 最后成员 → 截止期 = 50000+15000 = 65000 |
| 65000 | （定时器） | 无新报告 → 组删除，reason=last_member_timeout |

转发保留区间：`[0, 65000)`。要点：单个离开（t=40000）不得删组。

## s2_lost_query —— 查询丢失

- t=0 report → expiry 210000；t=100000 general query（世代→1）无响应（丢失）。
- 组在 t=210000 因 membership_timeout 过期（不是查询时刻起算）。
- t=250000 另一组 239.1.1.2 report gen=1 → 建组，expiry = 460000，run_until=300000 时仍存活（end=null）。

## s3_leave_rejoin —— 离开后重新加入

- t=0 report → expiry 210000；t=50000 leave（唯一成员）→ 最后成员阶段，截止 65000。
- t=60000 report（在 65000 之前）→ 挽救，expiry = 60000+210000 = 270000。
- t=270000 过期。区间 `[0, 270000)`，中间不中断。

## s4_boundary_timeout —— 边界超时（闭区间到期）

- 组 239.1.1.1：t=0 report → expiry 210000。t=210000 的 report 到达**之前**，
  组恰好在 210000 到期（闭区间）→ 先 `group_expired`，随后 report 重新建组，
  expiry = 420000。产生**两段**区间 `[0,210000)`、`[210000,420000)`。
- 组 239.1.1.2：t=209999 的 report 比截止期早 1ms → 仍存活，刷新到 419999。
  区间 `[0, 419999)`。
- 两者在 run_until=500000 前分别于 419999、420000 过期。

## s5_stale_generation —— 旧轮响应不覆盖新轮

- t=100000 query → 世代 1；t=150000 report gen=1 → expiry = 360000。
- t=200000 query → 世代 2；t=205000 report **gen=1**（旧轮迟到）→ stale，忽略，
  expiry 保持 360000（若被错误刷新会变成 415000）。
- t=250000 report gen=2 → expiry = 460000 → t=460000 过期。区间 `[0,460000)`。
- 组 239.1.1.2 在 t=210000 过期；t=220000 的 report gen=0（世代已为 2）→ stale，
  **不得复活**已过期组。区间仅 `[0,210000)`。

## s6_rejections —— 失败类别

事件 0..9 中，0（未声明接口）、1（非组播组）、2（成员非 IP）、3（组无状态时
leave）、5（非成员 leave）、6（对无状态组发组查询）、7（应答未来世代 gen=5 >
已发 0 轮）、9（时间倒退 50 < 100）分别被拒，类别见 expect。合法事件 4、8
使组 239.1.1.1 存活至 100+210000 = 210100。
