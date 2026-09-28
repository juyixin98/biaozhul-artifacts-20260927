# wtio — Weak Trace Inclusion Checker

检查两个**有限标号迁移系统（LTS）**之间的**弱迹包含（weak trace inclusion）**，
允许声明一个内部静默动作（silent / τ）。若实现可产生一条规格无法接受的可观察
迹，服务返回**最短**反例，并附带一条完全具体、可逐条边重放的实现接受运行，再
由一个与求解内核**零代码共享**的独立验证器复核。

技术栈：Rust + Axum + Serde（`Cargo.toml` 为依赖清单）。所有数据均为本地合成
夹具，无外部账号或真实业务数据。

---

## 1. 快速开始

```bash
cargo test            # 运行全部测试（求解内核 / 差异随机 / 错误契约 / 证据 / HTTP）
./demo.sh             # 本地端到端演示：CLI 全夹具 + 起服务 + curl + 诊断日志
```

单独使用：

```bash
cargo run -- serve --bind 127.0.0.1:8080      # HTTP 服务
cargo run -- check fixtures/vending_machine.json   # 单次检查，输出 JSON
cargo run -- verify fixtures/vending_machine.json  # 只做独立证据复核
```

CLI 退出码：`0` included；`10` not_included（已打印反例）；`20` unknown
（触发资源界）；`1` 输入/状态错误；`2` 内部错误。

HTTP：

```bash
curl -s 127.0.0.1:8080/health
curl -s -X POST 127.0.0.1:8080/api/v1/check \
  -H 'content-type: application/json' \
  --data @fixtures/erroneous_extra_output.json
```

---

## 2. 输入语言

```jsonc
{
  "silent_action": "tau",            // 内部静默动作名，默认 "tau"
  "alphabet": ["a", "b"],            // 可观察字母表；省略则取两侧可观察动作的并集（排序）
  "specification":   { "LtsDef": "..." },
  "implementation":  { "LtsDef": "..." },
  "limits": { "max_states_per_lts": 100000, "max_transitions_per_lts": 500000,
              "max_explored_pairs": 2000000, "max_witness_edges": 1000000 }
}
```

`LtsDef`：

```jsonc
{
  "name": "Impl",
  "initial": "i0",
  "states": ["i0", "i1"],            // 可省略：由 initial 与迁移端点推导
  "accepting": ["i1"],               // 省略/null = 所有状态接受；[] = 无接受态
  "transitions": [
    {"from": "i0", "action": "tau", "to": "i1"},
    {"from": "i1", "action": "a",   "to": "i0"}
  ]
}
```

标签为 `silent_action` 的边是内部步骤，其余必须在对齐后的可观察字母表中。
显式字母表即使只被一侧使用也会被保留（如实现多了一个规格没有的动作——这本身
就构成反例）。响应里的 `aligned_alphabet` 明确回显实际对齐结果。

---

## 3. 方法（以及它为什么正确）

静默闭包：对每个状态 `s` 求 ε(s) = 经零或多条 τ 边可达的状态集合；同时保存
BFS 父指针，任何「s 经 τ 到 t」的声明都能重建成具体边序列（`src/closure.rs`）。

确定化：带静默的 LTS 先做 ε-闭包确定化，宏状态是 ε-闭包集合：

* 初始宏状态 `I₀ = ε(initial)`；
* 弱后继 `weak_post(S, a) = ε({ t | q -a→ t, q ∈ S })`。

在「(规格宏状态, 实现宏状态)」对的乘积上做 BFS。一个节点是反例当且仅当
**实现宏状态含接受态而规格宏状态不含**。按可观察迹长度 BFS、同层按字母序展开，
保证返回**最短且确定**的反例（`src/solver.rs`）。

**不把单步动作相同当等价**：两个状态即使出边标号完全一致，只要接受性或未来
行为不同就绝不合并——这正是子集/宏状态构造所跟踪的。夹具
`acceptance_diff_via_marks.json` 专门钉住这一点。

**状态爆炸 → unknown**：任一界被触及（探索对数、宏集合大小、证据边长）都返回
`verdict: "unknown"` 并给出原因与部分统计，**绝不**伪造一个 `not_included`。

证据链：`src/witness.rs` 用自己的 DFS（只用闭包可达性和原始边）把最短可观察
词重放成「τ\*/a/τ\*」交替的具体接受运行；`src/verifier.rs` 再独立重算闭包与
接受性，逐条边核对该运行，并分别回答两侧是否接受该词。两者与求解内核都不共享
搜索代码。

---

## 4. 错误语义

四类错误贯穿输入语言、求解内核、证据与 HTTP，调用方可以明确区分：

| kind (`ErrorKind`)   | 含义                            | HTTP | 典型 code |
|----------------------|---------------------------------|------|-----------|
| `input_error`        | JSON/名字/字母表非法            | 400  | `invalid_json`, `empty_action`, `empty_alphabet`, `action_not_in_alphabet` |
| `state_conflict`     | 结构合法但模型自相矛盾          | 409  | `unknown_state`, `duplicate_state`, `silent_also_observable`, `duplicate_alphabet_action` |
| `resource_exhausted` | 输入超限（求解前）              | 413  | `too_many_states`, `too_many_transitions` |
| `computation_failed` | 不应发生的内部不变量破坏        | 500  | `witness_reconstruction_failed`, `closure_inconsistent` |

注意区分两种「耗尽」：

* **编译前**输入超过 `max_states_per_lts` / `max_transitions_per_lts` →
  HTTP **413** 的 `resource_exhausted` 错误；
* **搜索中**超过 `max_explored_pairs` / 宏集合大小 / 证据边预算 →
  HTTP **200**，正文 `verdict: "unknown"` 且带 `unknown.reason`
  （`pair_limit` / `set_size_limit` / `witness_limit`）。问题没有被回答，
  而不是被回答成「不包含」。

错误体形状：

```json
{ "error": { "kind": "state_conflict", "code": "unknown_state",
             "message": "Impl: transition source references state 'x' ..." } }
```

---

## 5. 输出（成功）

```jsonc
{
  "run_id": "run-1790518807406-0",
  "verdict": "not_included",          // included | not_included | unknown
  "silent_action": "tau",
  "aligned_alphabet": ["coffee", "coin", "tea"],
  "models": { "specification": {...}, "implementation": {...} },
  "counterexample": {
    "trace": ["coffee"],              // 最短可观察迹
    "implementation_replay": {        // 完全具体、可重放的接受运行
      "start_state": "boot",
      "hops": [{
        "before_tau": [ {"state":"boot","edge_id":0,"action":"tau","target":"idle"},
                        {"state":"idle","edge_id":1,"action":"tau","target":"armed"} ],
        "action": "coffee",
        "observable_edge": {"state":"armed","edge_id":0,"action":"coffee","target":"done"},
        "after_tau": []
      }],
      "final_tau": [], "accepting_state": "done", "edge_count": 3
    },
    "spec_macro_path": [["idle"], []],
    "impl_macro_path": [["boot","idle","armed"], ["done"]],
    "reason": "implementation accepts ... but specification can only be in ..."
  },
  "verification": {                   // 独立验证器的结论
    "implementation_run_valid": true,
    "specification_accepts_trace": false,
    "implementation_accepts_trace": true,
    "confirmed": true, "problems": []
  },
  "stats": { "explored_pairs": 4, "transitions_followed": 12, "...": "..." },
  "log_file": null
}
```

`POST /api/v1/verify-replay` 接收 `{model, trace, replay}`，只做独立复核、不跑
求解器，可用来校验外部产生或被篡改过的证据。

---

## 6. 诊断与重放

每次检查（CLI 或服务）都有 `run-<unix_ms>-<counter>` 的 **run id**。设置环境
变量即可持久化结构化运行日志：

```bash
WTIO_LOG_DIR=./demo-logs WTIO_LOG_STDERR=1 cargo run -- check <req.json>
```

每个 run 一个 `<run_id>.jsonl`，按序号记录可重放问题的关键中间状态与判断理由：

1. `request_received` — 两侧名字、静默动作、字母表是否显式；
2. `compiled` — 对齐后字母表、两侧状态/迁移数；
3. `solver_done` — 判定与统计（探索对数、宏状态数、宏集合峰值、跟随边数）；
4. `counterexample_verified` — 反例词、宏状态路径、独立验证是否确认及问题列表。

响应中的 `log_file` 给出该 run 的日志路径。

---

## 7. 测试与复现

```bash
cargo test
```

* `tests/solver_tests.rs` — 隐藏内部步骤、错误额外输出、不可达分支（普通与
  τ 后）、同单步动作但接受性不同、最短反例、空迹反例、资源界 unknown；并对
  每个固定夹具用独立穷举预言机短迹穷举对照。
* `tests/differential_tests.rs` — 固定种子随机合成 300 个带 τ、带显式接受集的
  小 LTS，求解内核与**独立 BFS 穷举预言机**（`src/oracle.rs`，不调用内核）
  逐案核对判定与最短词；并断言两类结果都确实出现（避免空泛通过）。
* `tests/error_contract_tests.rs` — 断言**具体**错误类别与稳定 code。
* `tests/verifier_tests.rs` — 真证据确认；六类被篡改的证据（错初态、改动作、
  伪造边号、迹与重放不符、τ 边冒充可观察边、外来动作）必须被独立验证器逐条
  拒绝并给出具体问题。
* `tests/api_tests.rs` — 路由、状态码（200/400/409/413/404）、unknown 为 200、
  verify-replay 往返，以及运行日志落盘且含可重放事件。

测试断言的是**具体结果和失败类别**，不是「接口能调用」。参考答案来自穷举预言
机而非被测核心自身。

开发过程中差异测试实际抓到并修复过两处缺陷，均在提交版本中保留回归覆盖：

1. 预言机最初把「规格接受、实现不接受」的反向差异也当作终止条件，从而掩盖后续
   真正的 impl-only 反例（seed 5）；已改为只在 impl-only 时终止、反向差异继续
   BFS。
2. 证人重建 DFS 的基例只判断「词已匹配完」而未判断「落停态能否经 τ 到达接受
   态」，导致选错可观察边后不回溯（seed 24）；已改为基例即检查可达接受态。

复现单个随机种子可参考 `tests/differential_tests.rs` 的 `synthesize(seed)`。

---

## 8. 模块边界

| 模块 | 职责边界 |
|------|----------|
| `src/input.rs` | JSON 输入语言与请求形状 |
| `src/compiler.rs` | 名字驻留、校验、显式字母表对齐、大小界 |
| `src/model.rs` | 稠密的求解侧表示（状态/标号 id） |
| `src/closure.rs` | τ 闭包 + 可重放 BFS 父指针映射 |
| `src/solver.rs` | 确定化乘积 BFS 求解内核、最短反例、unknown 界 |
| `src/witness.rs` | 独立的具体接受运行重建（τ\*/a/τ\*） |
| `src/verifier.rs` | 独立证据逐条边重放 + 两侧接受性交叉核对 |
| `src/oracle.rs` | 短迹穷举预言机（仅测试对照） |
| `src/engine.rs` | 编排：编译→求解→验证→响应，统一数据/错误契约 |
| `src/api.rs` | Axum HTTP 后端与错误状态码映射 |
| `src/diagnostics.rs` | run id 与可重放结构化日志 |

夹具在 `fixtures/`，每个文件带 `case_id` 与 `description`。
