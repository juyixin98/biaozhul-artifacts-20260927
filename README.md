# 本地有限状态机 · 显式状态模型检查器

对本地、有限域的状态机规范做**显式状态**模型检查，支持 CTL 的 **AG 安全性**与
**EF 可达性**子集，外加独立的死锁检查。全部数据为本地合成夹具或本地依赖，
不需要任何生产账号或真实业务数据。

技术栈：Rust（edition 2021）+ Axum + Serde。所有依赖版本钉在本地注册表缓存
中，可完全离线构建（`cargo build --offline`）。

---

## 1. 核心约束如何落实

| 约束 | 落实方式 |
|---|---|
| 声明状态变量范围 | 变量域为 `bool`、闭区间整数 `int[lo..hi]` 或 `enum{...}`；构建期检查非空、不超编码容量 |
| 转移守卫 | 每个转移有布尔 `guard`；非布尔在构建期按 `type_mismatch` 拒绝 |
| 同一前态更新 | 转移体是**并行赋值**：先对**同一个前态**求值所有 RHS，再统一提交。`x:=y, y:=x` 是交换而非覆盖（`fsm-lang` 语义模块 + `swap` 夹具 + 单测） |
| 状态规范编码后去重 | 混合进制（mixed-radix）把状态向量双射到单个 `u64`，无哈希碰撞；BFS 用该码去重 |
| 最短反例路径 | 广度优先 + 每状态一条父边，首次发现的违规即最短路径；`evidence.length` 为转移数 |
| 死锁 vs 合法终止分开 | 无可用守卫转移**且**不满足 `terminal` 才算死锁；满足 `terminal` 的静止态记为 `terminal_states`，永不判为死锁（计数器两套夹具 + 验证器 `final_terminal` 失败码） |
| 超预算给未知 + 统计 | 预算约束“已消费的不同状态数”；未闭合时每个未决性质为 `unknown`，理由文本显式写明“**NOT proven**”，绝不在没找到反例时宣称证明 |
| 多模块、非硬编码演示 | 输入语言 / 求解内核 / 证据验证 / 后端接口 / 夹具 / 独立测试 各自成 crate，见 §5 |
| 独立参考答案 | 手算 oracle 在 `fsm-fixtures::answers`，**不调用被测内核**；测试据此断言具体状态数、路径长度、失败类别 |
| 证据可独立验证 | `fsm-verify` 不调用内核，自行解码初态、逐条重放守卫与并行更新、复核终态；测试对真实证据做多种篡改，断言各自的失败码 |

---

## 2. 本地验证命令与预期判断

### 2.1 一键测试

```bash
cargo test --offline
```

预期：全部通过（当前 **39** 个测试，0 失败）。其中：

- `fsm-lang`：构建错误分类（9 项）、编解码双射往返、并行赋值交换、未赋值变量保留、除零不 panic、越域更新拒绝；
- `fsm-blackbox`（**23** 项独立黑盒测试）：
  - 互斥协议正确版 `holds`、缺陷版 `violated` 且最短反例长度 = 2，并**逐状态手工回放**；
  - 受限计数器：合法终止不计死锁；去掉 terminal 后在 `x=3` 死锁，路径长度 3；
  - **无初态**：运行级错误 `no_initial_state`；
  - **预算截断**（5001 空间 / 预算 500）：AG 与远处 EF 为 `unknown`，预算内的近处 EF 仍给出长度 10 的证据；
  - 证据篡改：伪初态、跳过一条边、不存在的转移名、终态其实不违规、`length` 谎报、把合法终止态当死锁——分别命中具体 `FailCode`；
  - HTTP 层：请求 ID 关联、版本/处理位置、错误类别、截断单列、/verify 接受真实证据。

> 判断标准不是“接口能调通”，而是对**具体数值与失败类别**的断言。

### 2.2 静态检查

```bash
cargo clippy --offline --all-targets   # 预期：无 warning/error
```

### 2.3 CLI（无需起服务）

```bash
cargo build --offline -p fsm-api --release
BIN=./target/release/fsm-check

# 缺陷互斥协议：预期退出码 1，conclusion=violated，反例 length=2
$BIN check --fixture mutex_bad --ag '!(in1 && in2)'

# 正确互斥协议：预期退出码 0，holds
$BIN check --fixture mutex_safe --ag '!(in1 && in2)'

# 无初态：预期退出码 2，status=error，error.kind=no_initial_state
$BIN check --fixture no_init --ag 'x < 3'

# 合法终止 vs 死锁
$BIN check --fixture counter          --ef 'x==5' --ef 'x==3'   # terminal=1, deadlocked=0
$BIN check --fixture counter_deadlock                            # deadlock_found=true, length=3

# 预算截断：ag 与远处 ef=unknown，近处 ef(x==10)=violated
$BIN check --fixture big_counter --ag 'x<=5000' --ef 'x==5000' --ef 'x==10' \
     --max-states 500 --no-deadlock

# 独立证据回放（手写证据，见仓库 examples/）
$BIN verify examples/deadlock_counter.fsm examples/deadlock_counter.evidence.json   # 退出码 0
$BIN verify examples/deadlock_counter.fsm examples/tampered_jump.evidence.json      # 退出码 1，update_mismatch
```

退出码约定：`0` 无违规；`1` 至少一条性质被违反（含死锁/证据不被接受）；`2` 运行级错误（如无初态、求值错误）。

### 2.4 HTTP 服务

```bash
FSM_BIND=127.0.0.1:8080 RUST_LOG=info ./target/release/fsm-check serve
# 另一终端
curl -s localhost:8080/health
curl -s localhost:8080/api/v1/version
curl -s -X POST localhost:8080/api/v1/check \
  -H 'content-type: application/json' \
  -H 'x-request-id: corr-777' \
  -d '{"fixture":"mutex_bad",
       "properties":[{"name":"mutex","kind":"ag","expr":"!(in1 && in2)"}]}'
```

- 响应头与 JSON 体都带 `x-request-id`/`request_id`（传入则沿用，否则本地合成）；
- `processing.location` 标明处理位置（`fsm-core::run_check` / `fsm-verify::verify`），
  `/api/v1/version` 给出各 crate 版本；
- 失败原因在独立的 `error` 字段（构建/运行错误），不确定结论在性质结果里以
  `conclusion:"unknown"` 单列，日志对每条 unknown 额外打 `UNCERTAIN` 行。

端点：`GET /health`、`GET /api/v1/version`、`GET /api/v1/fixtures`、
`POST /api/v1/check`、`POST /api/v1/verify`。

---

## 3. 输入语言（两种等价格式）

文本 DSL（节选）：

```text
system mutex_bad {
  var { in1: bool; in2: bool; locked: bool }
  init { in1 := false, in2 := false, locked := false }   // 具体初态
  transition t1_enter { guard: !in1;            then: in1 := true, locked := true }
  transition t2_enter { guard: !in2 && !locked; then: in2 := true, locked := true }
  transition t1_leave { guard: in1;             then: in1 := false, locked := false }
  transition t2_leave { guard: in2;             then: in2 := false, locked := false }
}
```

- `init { <布尔表达式> }` 为初态**谓词**（多初态）；`init { x := c, ... }` 为具体初态（必须逐变量给常量，走快速路径，不做全积扫描）。
- 表达式：布尔/整数字面量、`true/false`、枚举名、`( )`、一元 `- !`/`not`、
  `* / mod`、`+ -`、`< <= > >= == !=`、`&&`、`||`、`if c then a else b`；`//` 行注释。
- `terminal { <布尔表达式> }` 可出现多次，之间取或。
- 整数算术溢出、除零、越域赋值在求值期作为结构化 `EvalError`（不 panic）。

等价 JSON 形态见 `fsm-lang::json` 与 `examples/counter.json`；枚举初值可用变体名字符串。

---

## 4. 算法假设与语义

1. **有限域**：每个变量域有限，构建期要求混合进制乘积能放入 `u64`；超出按
   `state_space_overflow` 拒绝（而不是静默截断）。这是显式状态枚举可判定的前提。
2. **初始状态**：
   - 具体初态：单根，不做全积扫描（因此 `int[0..1_000_000_000]` + 具体初态也能立刻开搜）；
   - 初态谓词：按规范顺序扫描整个有限积判定“是否存在初态”，因此“无初态”不受预算影响、
     总能判定；扫描谓词候选**不**消耗状态预算，只有被选中的初态计数。
3. **预算语义**：`max_states` 约束“已消费的不同状态数”（初态 + 新发现后继）。
   预算耗尽时队列里可能还有未展开状态 → 可达性**未闭合** → 未决性质一律 `unknown`，
   并返回探索统计（消费/展开/求值转移/取边/初态数/候选扫描数/终止数/死锁数/耗时）。
   已找到的证据即使在截断时也保留。
4. **最短性**：BFS 按深度展开，每状态仅记录第一条父边，故任一反例/可达证据都是
   转移数最少的；路径由父链重建。
5. **并行更新**：RHS 全部针对前态求值后再提交，赋值表内顺序无关，未被赋值的变量保留前态值。
6. **CTL 子集读法**：
   - `AG p`：所有可达态满足 p；找到反例 → `violated`；全闭合无反例 → `holds`；未闭合 → `unknown`。
   - `EF p`：存在可达态满足 p；找到 → `violated`（携带可达证据，表示“坏状态可达”）；
     全闭合且无目标 → `holds`（即“错误状态不可达”）；未闭合 → `unknown`。
7. **死锁**：可达、非终止、且零个守卫为真。合法终止态不算死锁。
8. **证据可信边界**：内核输出的证据只是“主张”；`fsm-verify` 用规范独立重放，
   篡改会被对应失败码拒绝。验证是 sound 的（接受 ⇒ 确实是一条合法证据）。

---

## 5. 模块关系

```
                        ┌──────────────────────┐
                        │     fsm-fixtures      │ 本地合成夹具 + 手算 oracle(answers)
                        └───────────┬──────────┘
                                    │ 仅文本/JSON
                 ┌──────────────────┼─────────────────────┐
                 ▼                  ▼                      ▼
        ┌────────────────┐  ┌────────────────┐     ┌────────────────┐
        │    fsm-lang     │  │    fsm-core    │     │   fsm-verify   │
        │ 词法/语法/类型   │◄─┤ 预算化 BFS 内核 │     │ 独立证据重放     │
        │ 求值/并行更新    │  │ AG/EF/死锁/统计 │     │ (不调用 core)   │
        │ 混合进制编码     │  └───────┬────────┘     └───────┬────────┘
        └────────┬───────┘          │                      │
                 └──────────────────┼──────────────────────┘
                                    ▼
                          ┌────────────────────┐
                          │      fsm-api        │ Axum 路由 + CLI + 请求ID/日志/配置
                          └─────────┬──────────┘
                                    ▼
                          tests/fsm-blackbox（独立黑盒测试，oracle 不取自被测内核）
```

- `crates/fsm-lang`：输入语言（lexer/parser/json）、AST、构建期名字解析与类型检查、
  常量初态、表达式求值（短路、溢出/除零错误）、并行转移语义、混合进制编解码。
- `crates/fsm-core`：求解内核。初态枚举、去重 BFS、父链重建、AG/EF/死锁结论、预算与统计、
  结构化运行错误与可解释 trace。
- `crates/fsm-verify`：证据验证器。独立解码/重放/复核，输出 `VerificationReport` 与 `FailCode`。
- `crates/fsm-fixtures`：互斥（正确/缺陷）、计数器（终止/死锁）、无初态、大空间截断、
  并行交换、JSON 夹具，以及**手工推导**的期望答案。
- `crates/fsm-api`：Axum HTTP 层（请求 ID 中间件、路由、错误信封）+ `fsm-check` CLI + 环境配置。
- `tests/fsm-blackbox`：独立测试包；另有各 crate 内联单测与 `fsm-lang/tests/build_errors.rs`。

---

## 6. 依赖版本（钉死，离线可用）

| crate | 版本 | 用途 |
|---|---|---|
| rust edition | 2021（工具链 rustc/cargo 1.98.1 实测） | |
| serde | `=1.0.229`（derive） | 序列化 |
| serde_json | `=1.0.151` | JSON 输入/输出 |
| axum | `=0.8.9` | HTTP |
| tokio | `=1.53.1`（full） | 异步运行时 |
| tower | `=0.5.3`（util，测试 oneshot） | 服务组装 |
| tracing / tracing-subscriber | `=0.1.44` / `=0.3.23` | 结构化日志 |
| anyhow | `=1.0.104` | 仅 CLI 主流程错误 |

内部 crate 均为 `0.1.0`，以 path 依赖组成一个 workspace。版本钉在根 `Cargo.toml`
的 `[workspace.dependencies]`，对应当前机器 cargo 本地缓存；更换环境时放开为普通
semver 要求即可在线解析。

配置（环境变量，带默认值）：`FSM_BIND`（默认 `127.0.0.1:8080`）、
`FSM_MAX_STATES`（默认 `100000`）、`RUST_LOG`（默认 `info,fsm_api=debug`）。

---

## 7. 测试结果如实标记

- 已运行并通过：`cargo test --offline`（39/39）、`cargo clippy --offline --all-targets`（零告警）、
  release 构建、CLI 全部场景、HTTP 端到端（curl）、独立证据验证/篡改。
- 未运行：无。本项目不依赖外部服务，未做跨平台（Windows/macOS）与压测；这些不在本次范围内。
