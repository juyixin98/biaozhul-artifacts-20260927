# 架构与设计说明

## 1. 分层与契约

```
HTTP/CLI 边界 ── model.Packet ──▶ nat.Engine ──▶ StateStore(SQLite | memstore)
                     │                │
                     ▼                ▼
              model.Decision     model.Mapping / model.Event
```

- `internal/model` 是唯一的跨层数据/错误契约。任何层都不得自造五元组/判决结构或
  临时字符串错误码；失败统一表达为四种类别（`Category`）加稳定错误码（`Code`）。
- `nat.StateStore` 是接口，`store`（SQLite）与 `memstore`（内存）是两个实现。引擎
  不感知持久化细节；同一套引擎测试在两个实现上运行，防止"测试即被测实现"。
- 配置只在 `config` 解析一次，下游拿到的是已校验的 `Config`（公网地址、内网前缀、
  端口范围、各态 TTL）。

## 2. 时间模型：单调高水位时钟

每个 run 一个 `MonotonicClock`：

- 首个时间戳初始化时钟，不算回拨；
- `Advance(ts)` → `now = max(highWater, ts)`，仅当 `ts < highWater` 置 `rewind`；
- 每包处理顺序固定为：推进时钟 → **过期清扫**（`expires_at <= now`）→ 查表 → 状态机。

后果（也是测试锁定的不变量）：

1. 过期判定永远基于"生效时钟"，乱序/迟到的旧时间戳无法把时钟往回拨；
2. 旧时间戳因此**不能刷新或复活**任何已过期映射；
3. 被清扫端口会立刻归还端口池，随后（即使时间戳更老）的新流看到的是新状态——旧对端
   的迟到返回要么打到新主上被 `ENDPOINT_FILTERED`，要么该端口暂无主而
   `INBOUND_NO_MAPPING`/`LATE_RETURN_EXPIRED`。

时钟高水位也持久化在 `runs.high_water`，进程重启后重建。

## 3. 端口分配

`PortPool` 显式维护：`used`（当前被活跃映射拥有的端口集合）、`free`（已释放空洞的
最小堆）、`next`（从未发放的最低候选）。

- 分配：先复用最低空洞，再发放新端口 → 确定的"最低空闲"策略；
- 活跃端口绝不出现在可分配集合中；
- `Release` 仅由"过期清扫 / RST / 完整 FIN 关闭"触发；
- `Burn` 用于进程重启时从持久化的活跃映射重建占用集合。

引擎用一把 `sync.Mutex` 串行化"查映射→取端口→写存储"，所以并发首包不会双重分配；
SQLite 侧另有部分唯一索引兜底（见下）。

## 4. TCP 状态机

入站/出站各一张转移表（`fsmInbound`/`fsmOutbound`），状态：

`syn_sent → syn_ack_rcvd → established → fin_wait → closed`（UDP 恒为 `open`）。

- 未知流的出站 TCP 必须是裸 SYN，否则 `TCP_NON_SYN_OUTBOUND`；
- 不符合当前状态的标志组合 → `TCP_BAD_STATE`（如 `syn_sent` 收到裸 ACK）；
- 匹配对端的 RST 立即关闭并（默认配置）释放端口；
- FIN 交换完整（双方 FIN/ACK 完成）后关闭；
- 重传的 SYN / SYN+ACK 幂等接受。

UDP 无握手：任何匹配端点的包刷新 TTL。

## 5. SQLite 模式与一致性

- 单写连接（`SetMaxOpenConns(1)`）配合每 run 引擎锁，消除 SQLITE_BUSY；
- 关键约束为**部分唯一索引**：
  - `(run_id, protocol, src_ip, src_port, dst_ip, dst_port) WHERE state <> 'closed'`
  - `(run_id, protocol, mapped_port) WHERE state <> 'closed'`

  即同一活跃流/同一活跃外部端口在数据库层也不可能出现两行；关闭后历史保留、端口可
  重新发放。
- 过期清扫把到期行置为 `closed`（保留历史，用于区分"迟到返回"与"从未拥有"）；
- 事件表逐包追加，包体以 JSON 原样存储，保证回放时输入可还原。

## 6. 回放与"独立预言"

夹具是手写 JSON（`testdata/fixtures/`），只导入 `model` 的数据类型，**不导入引擎
内部**；每包的 `expect`（接受与否、类别、错误码、端口、状态、是否回拨、清扫数）由人
依据本文件的规则推导，是外部预言。

- 不同 `group` 按组号顺序执行；同一 `group` 内多包**并发**执行（专门用于首包竞赛，
  端口因此只在脚本级"多重集/唯一性"不变量上断言，逐包端口不写死）。
- 脚本级不变量：接受数、各错误码计数、每映射去重后的端口多重集、以及**回读存储**得到
  的最终活跃端口唯一性（回读存储是唯一能看到"清扫关闭"的来源，不能只看判决流）。
- 参考答案不全部由被测核心生成：夹具预言人工编写；另一部分断言（端口池、时钟、状态机、
  SQL 约束、HTTP 状态码）由不包含 NAT 判定逻辑的独立单元/集成测试给出。

## 7. 刻意不支持 / 边界

- ICMP 等非 TCP/UDP 协议：`UNSUPPORTED_PROTOCOL`；
- IP 分片：仅重组后输入，非首片/MF 一律拒绝；
- 不做真实报文收发、不碰 iptables/路由/网卡，全部是 `127.0.0.1` 上的元数据建模；
- 端点相关（对称型）NAT；地址受限/端口受限锥形等可在 `inbound.go` 的过滤策略处扩展。
