# 整数差约束集合服务（x − y ≤ c）

Rust + Axum + Serde 实现的整数差约束（difference constraints）集合服务：
支持**命名约束**、**可行解计算**与**冲突解释**（把不可满足性证明为一个完全由原约束 ID
构成的严格负环）。所有依赖均可在本地声明与启动，输入全部为合成数据。

## 它解决什么问题

每条约束形如 **`x - y <= c`**（`x`、`y` 为变量名，`c` 为 64 位有界整数）。

- 翻译为带权图边 `y -> x`，权为 `c`；
- 约束系统可行 ⇔ 图中**不存在负环**；
- 存在负环 ⇒ 不可满足，负环上的原约束集合就是一份可复核的冲突证据；
- 不存在负环 ⇒ Bellman-Ford 从超源出发的最短路距离直接给出一组整数可行赋值。

## 快速开始

```bash
cargo run                 # 默认监听 127.0.0.1:8080（PORT/HOST 可覆盖）
./demo.sh                 # 一键构建 + 启动 + 九步端到端演示（PORT=8091 ./demo.sh）
cargo test                # 31 个测试（内核 / 证据验证 / HTTP 三层）
```

最小请求示例：

```bash
curl -s localhost:8080/v1/solve -H 'content-type: application/json' -d '{
  "constraints": [
    {"name":"k1","x":"a","y":"b","c":5},
    {"name":"k2","x":"b","y":"c","c":-2},
    {"name":"k3","x":"c","y":"a","c":1}
  ]
}'
# => 200 {"version":null,"assignment":{"a":0,"b":-2,"c":0}}
```

## HTTP 接口

| 方法/路径 | 作用 |
|---|---|
| `GET /health` | 健康检查 |
| `GET /v1/constraints` | 当前集合快照（版本号 + 全部约束） |
| `POST /v1/constraints:batch` | **原子**追加一批命名约束；不可满足则整批拒绝并返回负环证据 |
| `POST /v1/reset` | 用新集合原子替换当前集合（不可满足则拒绝，状态不变） |
| `DELETE /v1/constraints/{name}` | 删除一条命名约束（版本号 +1） |
| `GET /v1/solution` | 当前集合的一组整数可行赋值 |
| `POST /v1/solve` | 对一次性约束集合求解，**不修改**服务端状态 |
| `POST /v1/evidence/verify` | 独立复核“约束 ID 序列是否构成严格负环” |

所有响应带 `x-run-id` 头。请求可自带 `x-run-id` 用于重放；缺省由服务端生成 `r-000001`
风格的编号。错误体与服务端结构化日志使用同一编号。

## 错误语义（输入错误 / 状态冲突 / 资源耗尽 / 计算失败可区分）

统一错误体：`{"error":{"category":...,"message":...,"details":...,"run_id":...}}`

| category | HTTP | 触发条件 | 是否改变集合 |
|---|---|---|---|
| `input_error` | 400 | 非法 JSON / content-type、标识符规则、空字段、**同一批次内重名**、环为空或环内 ID 重复 | 否 |
| `state_conflict` | 409 | 约束名与已存在集合冲突；批次或 reset 会使集合**不可满足**（携带 `details.evidence` 负环）；对当前集合验证未知约束 ID | 否 |
| `resource_exhausted` | 507 | 变量数 > 128 或约束数 > 1024 | 否 |
| `computation_failure` | 422 | 松弛或环费用求和发生 **i64 整数加法溢出**；请求被明确拒绝，不回绕 | 否 |
| `not_found` | 404 | 删除/访问不存在的命名资源或路由 | 否 |
| `internal_error` | 500 | 内核不变量被破坏（理论上不应发生） | 否 |

不可满足冲突的响应示例（`state_conflict`）：

```json
{"error":{
  "category":"state_conflict",
  "message":"batch rejected: constraint set would be unsatisfiable",
  "details":{
    "conflict":"constraint_set_unsatisfiable",
    "evidence":{
      "cycle":[
        {"constraint":"n1","from":"q","to":"p","weight":-1},
        {"constraint":"n2","from":"p","to":"q","weight":-1}
      ],
      "total_cost":-2
    }
  },
  "run_id":"r-000006"
}}
```

## 关键设计

### 图、不连通分量与超源

约束 `x - y <= c` 翻译为边 `y -> x`（权 `c`）。求解时所有距离初始化为 0，
等价于增加一个向每个节点连 0 权边的**隐式超源**：每个连通分量都被同等扫描，
因此**位于远离“起点”的不连通分量中的负环同样会被发现**（测试
`negative_cycle_in_disconnected_component` 专门覆盖）。

超源边是内核内部的匿名边，不会被实例化为 `Edge`，因此**永远不会出现在证据中**；
证据只引用产生边的原约束下标，并映射为约束名。

### 负环取证与严格性

第 n 轮（检测轮）若仍有边可松弛，则按前驱链回溯：先沿前驱走 n 步落入环内，
再收集闭合环。环费用用 `i64::checked_add` 求和，且内核与独立验证模块都要求
`total_cost < 0`；零权环（如 `x-y<=0, y-x<=0`）判为可行。

### 证据独立验证

`src/evidence.rs` 的验证**不调用求解内核**：它直接从约束文本重算边，
检查 ID 是否存在、环是否按 `to == next.from` 闭合、费用是否严格为负。
内核产出的每条证据在返回前都会被该模块复核一遍；`POST /v1/evidence/verify`
则把同一能力开放给外部。结构不成立（不闭合/费用非负）返回 200 `valid:false`
并给出 `reason`；输入、状态、计算类问题仍走 4xx。

### 整数溢出

所有距离加法与环费用求和均为 checked 加法。例如
`a-b<=-1` 与 `x-a<=i64::MIN` 在首轮松弛计算 `-1 + i64::MIN` 时下溢，
返回 `422 computation_failure`（含溢出位置），不 panic、不回绕。

### 原子批次：读不到半更新集合

`ConstraintStore` 用一把写锁覆盖一次批次的
**输入校验 → 重名检查 → 配额检查 → 求解 → 提交**全过程：任一步失败都在写状态前返回，
读请求只能看到整批之前或整批之后的集合。集合带单调递增 `version`，
成功变更（含删除/reset）才会 +1；已提交集合恒为可满足。
计算密集或持锁操作经 `tokio::task::spawn_blocking` 执行，不阻塞异步运行时。

## 模块边界

```
src/
  main.rs            服务入口（绑定、日志初始化、优雅退出）
  lib.rs             模块装配
  model.rs           输入语言：请求/响应 DTO、标识符校验（数据契约）
  error.rs           错误分类与 HTTP 映射（错误契约）
  solver/
    graph.rs         约束 -> 图边、变量 intern、配额、内核错误类型
    bellman.rs       Bellman-Ford：可行性、赋值、负环取证、checked 算术、过程 trace
  evidence.rs        独立证据验证（绕过内核，直接按输入语言重算）
  store.rs           命名集合、版本、原子批次/reset/删除、内核证据复核
  api.rs             axum 路由、run-id 中间件、阻塞任务隔离、错误映射
tests/
  common/mod.rs      运行日志、独立回代校验器、确定性 RNG、HTTP 助手
  solver_kernel.rs   内核测试（手算答案/零权环/远端负环/自环/溢出/配额/随机交叉）
  evidence_verify.rs 证据验证测试
  api_http.rs        HTTP 端到端（错误类别/原子性/并发/run-id/reset）
```

## 测试与可重放日志

```bash
cargo test                                       # 运行全部 31 个测试
cargo test --test solver_kernel -- --nocapture   # 查看用例运行日志
```

测试日志形如：

```
[t-run001] BEGIN case=negative_cycle_in_disconnected_component
[t-run001] state cycle_edge_indices = [2, 1]
[t-run001] state cycle_constraint_names = ["n2", "n1"]
[t-run001] END ... verdict=unsat reason=negative cycle in disconnected component found ...
```

每个用例有稳定编号（写死的种子/编号，可重放），记录关键中间状态（边、轮次松弛数、
取环下标、环费用）与判定理由；服务端日志 (`demo-logs/server.log`) 以 run id 关联
请求、版本号、求解轮次与结论。

断言的**参考答案不来自被测核心**：

1. 关键用例给出**手算的具体赋值与环费用**（用例注释包含逐轮松弛过程），测试精确比对数值；
2. `common::check_assignment` 是朴素的逐约束回代器（独立实现），
   所有可行解都由它再次验证；
3. 冲突环由独立模块 `evidence::verify_cycle` 复核闭合性与严格负费用；
4. 随机交叉测试用写死种子的 LCG 生成 40 个小系统，对两类结论分别做上述独立核验。

配额默认：变量 ≤ 128、约束 ≤ 1024（`src/solver/graph.rs`，可按需调整后再测）。
