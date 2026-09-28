# diffconstraints — 整数差分约束服务

形如 `x - y <= c` 的**整数**约束集合服务：支持**命名约束**、**可行解求解**与
**冲突解释**。约束被归约为带权有向图，任意负环代表不可满足；冲突证据以
**原约束 ID 组成的负环**返回，绝不暴露内部匿名边。

技术栈：Rust + [Axum](https://github.com/tokio-rs/axum) 0.8 + Serde，单二进制、
内存状态、无外部数据库，所有输入均为本地合成输入。

---

## 1. 目录结构（模块边界与契约）

```
src/
├── error.rs       唯一错误契约：ErrorKind (input/state_conflict/
│                  resource_exhausted/computation_failed/not_found)
├── model.rs       经校验的领域对象 Constraint（x - y <= c，i64）
├── lang.rs        文本输入语言（c1: x - y <= 5，注释、行列错误）
├── graph.rs       约束 → 带权有向边（y→x 权 c），不连通分量（并查集）
├── solver.rs      Bellman–Ford 内核：checked 算术、负环提取、可重放 trace
├── evidence.rs    独立证据复核：赋值逐条核验 / 冲突环闭合且严格负
├── store.rs       命名约束存储；批处理在互斥锁内整体模拟→限额→提交
├── service.rs     编排：store 快照 → graph → solver → evidence 自检
├── http_api/      Axum 边界：mod.rs（路由/中间件/错误映射）+ dto.rs（线协议）
├── testlog.rs     测试运行日志（run id、输入、中间态、判断理由，JSONL）
└── main.rs        服务入口（BIND_ADDR，默认 127.0.0.1:8080，优雅关闭）
tests/
├── common/        mod.rs 真实 TCP 回环测试服务器 + 极简 std HTTP 客户端
│                  oracle.rs 独立预言机（穷举可行性、直接环核验），不调用被测内核
├── solver_kernel.rs  手算可行链 / 零权环 / 远离首节点的负环 / 溢出 / 空系统
├── service_layer.rs  文本批原子性、子集求解、trace 内容、revision、验证
├── http_api.rs       全 HTTP 流程 + 五种错误类别 + 原子替换回滚 + 限额
└── concurrency.rs    并发批处理与读取，断言读不到半更新集合
scripts/demo.sh  本地端到端演示（构建、启动、可行/冲突/各类错误）
```

模块间只通过显式类型通信：HTTP DTO 经 `Constraint::new` 校验后进入领域层；
求解器只依赖 `graph::Graph`；服务层在返回前用 `evidence` 对内核结果做独立自检。

## 2. 数学归约

约束 `x - y <= c` 等价于边 `y → x`、权 `c`（位势不等式
`d[x] <= d[y] + w(y,x)`）。

* 所有点初始距离置 0（等价于一个到所有点权为 0 的超级源），因此**不连通分量
  无需特殊处理**，每一环都被探索到。
* 松弛 `n` 轮仍可松弛 ⇒ 存在负环 ⇒ 系统不可满足；第 n 轮仍松弛的边触发前驱链
  回溯（先走 `n` 步落到环上，再绕环一周）恢复负环。
* 每条边记住来源约束 ID；**冲突环只含原约束 ID**。
* 所有路径和使用 `i64::checked_add`，一旦溢出返回 `computation_failed`，
  绝不回绕成一个假可行解。
* 上限：变量 4096、约束 8192、请求体 1 MiB。

## 3. 快速开始

```bash
cargo build --offline     # 依赖已在本地缓存时；否则 cargo build
cargo run                 # 监听 127.0.0.1:8080
# 或自定义端口：
BIND_ADDR=127.0.0.1:9000 cargo run
```

健康检查：

```bash
curl -s http://127.0.0.1:8080/v1/health
# {"status":"ok"}
```

一键本地演示（另起 18080 端口，覆盖可行→冲突→五类错误）：

```bash
./scripts/demo.sh
```

## 4. HTTP 接口

所有接口前缀 `/v1`，请求/响应均为 JSON。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 健康检查 |
| GET  | `/constraints` | 列出全部约束 + 当前 revision |
| POST | `/constraints` | 批量新增（JSON 数组和/或 `text` 文本块） |
| POST | `/constraints/replace` | **原子**替换：清空+装入，任一项失败则整体回滚 |
| POST | `/constraints/update` | 更新单个已存在约束 |
| POST | `/constraints/delete` | 删除单个约束 |
| POST | `/solve` | 求解（body 可空；可传 `{"only_ids":[...]}` 只解子集） |
| POST | `/verify` | 独立复核：`{"assignment":{...}}` 或 `{"cycle":[ids]}` |

约束 JSON：`{"id":"b_start","lhs":"b","rhs":"a","bound":5}` 表示 `b - a <= 5`。

文本 DSL（每行一个约束，支持整行/行尾 `#` 注释与空行）：

```text
# 作业调度：b 最早在 a 之后 0、最晚晚 5 个时间单位
b_start: b - a <= 5
c_start: c - b <= -2
```

`/solve` 成功响应（feasible）含一个具体赋值与逐轮 trace：

```jsonc
{
  "status": "feasible",
  "revision": 1,
  "variable_count": 3,
  "constraint_count": 3,
  "component_count": 1,
  "components": [ {"id":0,"vertices":[...],"edges":3} ],
  "witness": {
    "assignment": {"a": 0, "b": 0, "c": -2},
    "iterations": 3,
    "trace": {
      "passes": [ {"pass":1,"relaxations":2,"detail":{"distances":[...],"relaxed_edges":[...]}} ],
      "final_distances": [0,0,-2],
      "rationale": "pass 3 produced no relaxations; ..."
    }
  }
}
```

不可满足时（infeasible，`weight` 严格为负，顶点序列首尾相同）：

```jsonc
{
  "status": "infeasible",
  "conflict": {
    "cycle_constraint_ids": ["ab", "bc", "ca"],
    "cycle_vertices": ["a","b","c","a"],
    "weight": -1,
    "iterations": 3,
    "trace": { "passes": [...], "final_distances": [...], "rationale": "..." }
  }
}
```

## 5. 错误语义（唯一错误契约）

错误响应统一形如：

```json
{"error":{"kind":"state_conflict","message":"...","detail":{...}}}
```

| kind | HTTP | 何时产生 | 可重试方式 |
|---|---|---|---|
| `input` | 400 | JSON 非法/缺字段、标识符非法、文本 DSL 语法错（带行列）、常量超出 i64、验证赋值缺变量 | 修改请求体 |
| `state_conflict` | 409 | 新增 ID 已存在；更新/删除/求解子集引用未知 ID；同批内 ID 重复 | 改 ID 或先查现状 |
| `resource_exhausted` | 413 | 约束数 > 8192、变量数 > 4096、请求体 > 1 MiB | 缩小请求 |
| `computation_failed` | 422 | 松弛/环求和 i64 溢出；内核不变量被破坏（前驱链断裂等，属内部防御） | 收窄常数；内部错应报 bug |
| `not_found` | 404 | 路由不存在 | 修正路径 |

关键区分原则：**输入里写了一个超出 i64 的常数**是 `input`（在反序列化阶段
拒绝）；**输入合法，但求解过程中路径和溢出 i64** 是 `computation_failed`
（在内核 checked_add 处拒绝）。

### 原子批处理（读不到半更新集合）

存储由单把 `Mutex` 保护。`POST /constraints` 与 `/constraints/replace` 在
**同一把锁内**完成「在工作副本上模拟全部操作 → 校验后置规模 → 一次性提交」：

* 批中任何一项失败（含文本块第 20 行解析错）→ 状态逐字节不变，revision 不增；
* 读接口（list/solve/verify）在同一把锁下取一致快照，求解耗时部分在锁外基于
  该快照运行——并发批处理不可能被观察到一半。

## 6. 测试与复现

```bash
cargo test            # 36 个测试（12 单元 + 24 集成）
```

测试设计要点（不是“接口能调通”级别的断言）：

* **手算实例**：可行链（逐步验证每条不等式）、**零权环**（权恰好 0 可行且强制
  等式 `y=x+1`）、**远离初始节点且位于第二个不连通分量中的负环**（权 -1）、
  负自环；均断言**具体数值**。
* **独立预言机**：`tests/common/oracle.rs` 用有界整数箱穷举判定可行性、用
  独立的逐边追踪核验环闭合与环权，**完全不调用 Bellman–Ford**；预期答案不
  全部由被测核心自己生成。
* **失败类别**：400/409/413/422/404 逐一断言状态码与 `error.kind`；溢出断言
  消息含 `overflows i64`。
* **冲突证据**：断言环权严格负、顶点序列首尾闭合、只含给定原约束 ID、不含
  另一可行分量的边，并用 `/v1/verify` 与预言机双重复核。
* **原子性/并发**：失败替换后计数、内容、revision 全部回到旧值；多线程读写
  断言任何快照都自洽。

### 测试日志（可重放）

每次测试运行写一条 JSON 记录到 `test-results/runs.jsonl`（追加式），含：

* `run_id`（纳秒时间戳+测试名，同时打印到 stdout，可用它在文件中检索）；
* 原始输入（`events[].event = input`）；
* 关键中间状态：图边、连通分量、每轮松弛次数/距离向量、提取出的环、最终距离；
* `judgment` 及判断理由、观察到的错误类别、最终 `verdict`（pass/fail/
  expected_error）。

复现一次失败：

```bash
cargo test 2>&1 | grep testlog        # 找到 run id，例如 t-18d929..-negative_cycle..
grep 't-18d929' test-results/runs.jsonl | python3 -m json.tool
```

## 7. 依赖清单

见 `Cargo.toml`（含传递依赖锁定于 `Cargo.lock`）：axum、tokio、serde、
serde_json、tower、tracing/tracing-subscriber。测试不引入第三方 HTTP 客户端，
只用 `std::net::TcpStream`，避免测试预言机依赖被测 Web 栈。

## 8. 设计取舍与边界

* 状态仅在内存，进程重启清空；这是题目约定的本地合成输入环境。
* 标识符规则：字母/下划线开头，后续字母/数字/下划线。**不允许 `-`**，从而
  文本 DSL 中 `x-y` 在无空格时也能无歧义切分。
* 赋值验证对出现于赋值但未被任何约束引用的变量给出 `extra_variables` 提示，
  不算失败；缺少变量是 `input`。
