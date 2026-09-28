# btmon — 有限轨迹上的有界时序规则监视器

Rust + Axum + Serde 实现。对有限事件轨迹增量监视两类有界义务，给出
**三值结果**：满足 `sat` / 违反 `viol` / 等待 `wait`。轨迹未结束时，
`wait` 绝不会被当作通过。

## 规则语言

- **响应规则 respond-within-N**：触发后，响应事件必须落在闭窗口
  `[t+after, t+after+within-1]` 内。
  - `satisfy: all`（默认）：一个响应事件可同时满足它匹配到的**所有**待定义务；
  - `satisfy: one`：一个事件只配对最早合格的义务（其余继续等待）。
  - `on_close: strict`（默认）/ `lenient`：提前结束时剩余义务判违反 / 接受。
- **保持规则 sustain-for-K**：触发后，条件必须在闭窗口
  `[t+after, t+after+duration-1]` 的**每一步**都成立（duration 含首步）。
- 条件是原子合取 `all:[...]`；原子支持 `eq/ne/lt/le/ge/in`，缺字段/类型不符
  按不匹配处理。`kind` 是普通事件属性。

每次触发产生一个**独立义务实例**，id 为 `"{epoch}:{rule_id}:{trigger_step}"`。

## 三值与封闭语义

- 全局裁决 = 所有实例在格 `sat < wait < viol` 上的合并：任一违反即违反，
  否则任一等待即等待，否则满足。
- `end`（结束标记）封闭全部剩余义务：响应未响应 → `closed_pending`，
  保持窗口未观测完整 → `closed_incomplete`（strict），或 `closed_accepted`
  （lenient）。这些理由码与真正的 `deadline_missed` / `condition_failed`
  严格区分。
- `rotate` 换规则版本：在边界立即封闭旧 epoch 的待定义务并开启新 epoch；
  义务带 epoch 标签，**旧义务永不读取/匹配新版本事件，版本状态不混用**。
  轨迹时间轴连续，不回绕步号。

## 证据与恢复

- 每个状态转移写一条哈希链日志（`GENESIS` 起，SHA-256 over 规范化 JSON）。
- 快照携带整体摘要；恢复时校验摘要、哈希链与内部交叉引用：
  篡改内容 → `SNAPSHOT_DIGEST_MISMATCH`，伪造摘要但改日志 → `DECISION_CHAIN_BROKEN`，
  版本不符 → `VERSION_MISMATCH`。
- 恢复后继续监视的结论与不中断运行一致（有测试断言）。

## 模块边界

| 文件 | 职责 |
|---|---|
| `src/lang.rs` | 输入语言：事件、条件、两类规则、三值结果、理由码 |
| `src/error.rs` | 四类可区分错误契约 + HTTP 状态映射 + 统一错误体 |
| `src/canonical.rs` | 规范化 JSON / SHA-256 摘要 |
| `src/monitor.rs` | 增量求解内核：义务状态机、旋转、封闭、决策日志、快照 |
| `src/reference.rs` | 独立参考答案：集合式朴素重扫（与内核算法无共享） |
| `src/store.rs` | 内存监视器仓库 |
| `src/api.rs` | Axum HTTP 传输与 DTO |
| `src/main.rs` | `serve` 与离线 `replay` 双算法交叉核对 |

## 快速开始

```bash
cargo build --locked --release
./target/release/btmon replay fixtures/runs/03-missing-early.json   # wait -> viol
BTMON_BIND=127.0.0.1:8080 ./target/release/btmon serve
```

完整复现步骤、夹具表、错误码表与已保留的真实运行结果见
[REPRODUCE.md](./REPRODUCE.md)。

## HTTP 接口

`POST /monitors`、`GET /monitors/{id}`、`POST /monitors/{id}/events`
（单事件或 `{events,steps}` 批，批原子）、`POST /end`、`POST /rotate`、
`GET /obligations`、`GET /rules`、`GET /decisions?limit=n`、
`GET /snapshot`、`POST /restore`、`POST /verify`。示例请求体见
`examples/`；可运行脚本 `examples/curl-walkthrough.sh`。
