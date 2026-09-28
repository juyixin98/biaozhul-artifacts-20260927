# 架构

## 分层与依赖方向

```
cmd/*                  （进程边界：CLI 参数、信号、文件）
  │
  ├── internal/config     配置解析与校验
  ├── internal/service    HTTP API + 入库流水线（编排层）
  │      ├── internal/reassembly   重组状态机（纯逻辑，无 I/O）
  │      │      └── internal/netmodel  协议值类型 + 序号空间
  │      ├── internal/storage     SQLite 持久化
  │      └── internal/diagnose    结构化日志
  └── (replay 子命令直接走同一流水线原语)
```

`reassembly` 不 import 任何 I/O 包；`netmodel` 不依赖其他内部包。这保证重组核心可以
脱离存储与 HTTP 独立测试，也保证参考答案（`testsupport`）可以只依赖 `netmodel`。

## 序号空间与回绕（netmodel.SeqMapper）

TCP 序号是模 2^32 的环形值。比较采用 RFC 9293 §3.2.1 的半窗口规则：

```go
FwdDist(seq, ref) = int64(int32(seq - ref))   // (-2^31, 2^31]
```

正数表示 seq 在 ref 之后。恰好落在半窗口边界（差值 -2^31）判为“旧”，避免二义性。

为了把环形序号投影成单调坐标，引擎为每个方向记录若干**锚点**（SYN ISN、各段起点、
FIN 位置）：`abs = anchor.abs + FwdDist(raw, anchor.raw)`，查询时取环形距离最近的锚点。
多锚点使乱序与跨回绕片段各自就近投影，而不是依赖到达时的临时判断。

- 观测到 SYN：SYN 锚定内部坐标 0，SYN 消耗一个序号，首数据字节为内部坐标 1。
- 未观测到 SYN：首个观测数据字节锚定为 0；若之后到达更早的段，base 下移
  （存储坐标是单调内部值，视图统一减去 base，因此不会移动或重复已接受字节）。

对外暴露的所有坐标统一换算为“首应用字节 = 0”的相对偏移；原始 32 位序号在证据中保留。

**固有歧义**：若同一方向两次观测之间真实前进超过 2^31 字节，32 位序号本身无法判定先后，
任何实现都不能消除该歧义。夹具规模远小于该窗口；代码不假装能解决它。

## 状态机（reassembly）

- `Manager`：以规范化四元组 `FlowKey`（低地址端点为 A）为键，每个流持有多个 `Generation`。
- `Generation`：一次连接化身。新开局 SYN（无 ACK）在以下情况创建新世代：
  上一世代已 orderly close 或被 RST，或当前开放世代的开局 ISN 与该 SYN 不符。
  相同 ISN 的重复 SYN 记为 `SYN_DUPLICATE`，不创建世代。
- `DirectionState`（A→B、B→A 各一）：`SeqMapper` + `sparseBuffer` + FIN/RST 状态。
- 每个报文按**到达顺序**处理（服务端不按序号排序），依次经过握手解析、RST、数据、FIN。

### 数据接受规则（sparseBuffer.put）

对每个字节位置：

1. 空缺 → 接受（new）。
2. 已有且字节相同 → 计为 identical retransmission，**不产生任何新输出**。
3. 已有但字节不同 → 冲突，按策略处理：
   - `first_wins`：保留先到字节，新字节 rejected；
   - `last_wins`：新字节替换，旧字节作为被替换证据保留在冲突记录；
   - `quarantine`：新片段字节全部进入隔离清单（held bytes），流不变。

默认策略是最保守的 `quarantine`（配置可改）。

### 输出规则（证据约束）

- 只有从相对偏移 0 起、**逐字节连续**的前缀进入 `stream`。
- 缺口之后的已接受段放在 `held_out_of_order`，永不混入流。
- 缺口必须被证据夹逼才成立：后面存在已接受字节，或者 FIN 证明了流终点。
  没有 FIN 且后面没有证据的尾部只是“尚未收到”，不报告为缺口，也不补造长度。
- FIN 位置 = `seq + len(payload)`，FIN 自身再占一个序号；FIN 之后的数据按
  `DATA_AFTER_FIN_REJECTED` 拒绝。重复 FIN（同位置）记重传；矛盾 FIN（不同位置）记
  undecided 证据，不擅自选择。
- RST 关闭世代；之后无新开 SYN 的数据为 undecided 且不输出；新 SYN 可开新世代。

## 存储（SQLite）

单次入库在一个事务内写入：`requests`、`packets_archive`（不含载荷）、`events`、
`conflicts`、`generation_views`（JSON 快照）。之后所有回放读都来自存储，重读不改变分析状态。
SQLite 使用纯 Go 驱动 `modernc.org/sqlite`（WAL，单写连接），无 CGO 依赖。

## 诊断与脱敏

事件为结构化 JSON：`request_id`、`record_id`、`code`、`level`、`flow`、`generation`、
`direction`、`raw_seq`、相对坐标跨度、`next_contiguous`、`fin_pos` 以及一句
accept/reject/undecided 判定说明（`decision` 字段）。

应用字节被视为敏感数据：默认诊断日志绝不包含载荷；仅当配置显式 `payload_preview=true` 时
最多打印 16 字节十六进制预览并给出完整长度。`events` 与 `packets_archive` 表不保存原始
载荷；重组后的连续流作为世代视图快照保存在 `generation_views`（这是重启后仍能回放所必需
的，且仅通过按请求鉴权的回放接口显式读取）。服务默认只绑定 127.0.0.1。

## 扩展点

- 更大规模：把 `sparseBuffer` 换成区间树（rbtree of intervals）即可，`put/runs/gaps`
  是唯一依赖点。
- 更多输入格式：pcap-ng 解析器实现 `[]netmodel.Packet` 后可直接接入同一流水线。
