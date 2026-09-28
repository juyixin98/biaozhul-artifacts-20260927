# se — 定长整数小程序的有界符号执行服务

一个仅包含**定长整数、条件分支、有限循环**的小程序的符号执行 Web 服务。
技术栈：Rust · Axum · Serde；求解内核为成熟的 **Z3**（通过 SMT-LIB 2 / QF_BV
子进程接口对接），无需任何生产账号或外部业务数据。

核心设计原则：

* 每条路径维护独立的**符号状态与路径约束**，整数位宽固定为 8/16/32/64，
  溢出语义显式（`wrap` 回绕或 `trap` 失败）。
* 探索预算（路径数）与循环展开上限写入配置与每份结果报告。
* 求解器输出的反例**不被直接信任**：必须通过独立的具体解释器重放，复现
  *相同失败类别 + 相同语句位置*，才标记为 `confirmed`。
* **覆盖不到的路径返回 `unknown`，绝不返回安全**：求解器 `unknown`、路径预算
  耗尽、循环展开触顶都会形成显式 `cuts` 并把总体判定降为 `unknown`。
* 小域穷举 oracle 与一个测试专用的独立暴力求解器提供**不经过被测引擎**的
  参考答案。

---

## 目录结构（按层次组织，非单文件/壳工程）

```
crates/
  se-lang/         输入语言：AST、JSON 解析校验、定长位宽运算、
                   独立具体解释器（同时用于重放与穷举 oracle）
  se-solver/       求解内核：符号项 → SMT-LIB 2(QF_BV) → Z3 CLI 后端
  se-engine/       符号执行：路径状态/约束/工作列表、预算与展开、证据与审计日志
  se-verify/       证据验证（独立重放）+ 小域穷举 oracle
  se-server/       Axum HTTP 后端 + CLI + 配置层
  se-integration/  独立测试层（含独立暴力求解器、夹具、tests/ 全套断言）
configs/server.toml
examples/          示例程序、请求、curl 演示脚本
docs/              语言与架构文档
```

依赖全部在根 `Cargo.toml` 中以 `=x.y.z` **精确锁定**，并随仓库提交 `Cargo.lock`。

---

## 构建与运行

前置：Rust（1.75+；开发环境为 1.98.1）与 [`z3`](https://github.com/Z3Prover/z3)
可执行文件（开发环境实测 Z3 4.8.12）。不依赖 `libz3` 共享库——服务通过
`z3 -smt2 -in` 子进程通信。

```bash
cargo build --release
./target/release/se-server --config configs/server.toml
# 或直接用默认配置：
./target/release/se-server --bind 127.0.0.1 --port 8080
```

健康检查会报告后端是否就绪：

```bash
curl -s http://127.0.0.1:8080/health
# {"status":"ok","solver":"z3-cli","solver_version":"Z3 version 4.8.12 - 64 bit","solver_available":true}
```

配置优先级：CLI 参数 > 环境变量 > TOML 文件 > 内置默认值。环境变量：
`SE_BIND`、`SE_PORT`、`SE_Z3_BIN`、`SE_Z3_TIMEOUT_MS`、`SE_MAX_PATHS`、
`SE_MAX_LOOP_UNROLL`；日志可用 `SE_LOG=debug`。

---

## HTTP 接口

所有接口均为本地 JSON。异常不会被折叠成成功：请求错误返回结构化 4xx；
分析无法得出结论时 HTTP 仍为 200，但 `"verdict"` 为 `"unknown"` 且带 `cuts`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 存活与求解器可用性/版本 |
| GET | `/version` | 各组件版本 |
| POST | `/analyze` | 符号分析 + 独立证据重放（可选穷举 oracle） |
| POST | `/verify/replay` | 用具体解释器执行一组给定输入 |
| POST | `/oracle` | 小域穷举真值（有上界，超限 `unknown`） |

### `POST /analyze` 请求

```json
{
  "request_id": "doc-analyze-wrap",
  "max_paths": 256,
  "max_loop_unroll": 64,
  "enforce_domains": true,
  "with_oracle": true,
  "oracle_cap": 4096,
  "program": { "width": 8, "overflow": "wrap", "...": "见 docs/language.md" }
}
```

`examples/requests/analyze.json` 是完整可用样例：

```bash
curl -s -X POST http://127.0.0.1:8080/analyze \
  -H 'Content-Type: application/json' \
  --data @examples/requests/analyze.json
```

### 响应要点

* `request_id`（回显）、`run_id`、`program_id`（程序内容哈希）用于日志关联；
* `verdict`：`violation` / `holds` / `unknown`（已经过重放裁决）；
* `replay.verified[]`：每个候选反例的重放结果——`status=confirmed` 要求
  重放失败类别与语句位置完全一致；
* `report.budget`：`max_paths`、`explored_terminals`、`forked_branches`、
  sat/unsat/unknown 查询计数、`max_loop_unroll` 与实际最大展开数；
* `report.cuts[]`：未覆盖原因（`path_budget` / `loop_unroll` /
  `solver_unknown` / `solver_unavailable`）；
* `report.steps[]`：带序号的进度与判定日志（start/check/fork/violation/…），
  check 事件记录 sat/unsat/unknown 的判定依据；
* `oracle`（请求时）：穷举真值——总赋值数、完成数、被 assume 排除数、
  每个失败的类别/位置/输入；截断时 `truncated=true` 且 `verdict="unknown"`。

端到端演示（先启动服务）：

```bash
examples/curl-demo.sh
```

---

## 验证方式（已真实执行）

### 四类要求场景 + 小域穷举比对 + 反例位置复现

| 场景 | 夹具 | 断言内容（摘要） |
| --- | --- | --- |
| 断言失败 | `ASSERT_FAIL` (`assert x<=10`, u8) | 引擎判 violation、witness∈{11..255}、重放为 `assertion@0`；穷举 256 个赋值得 245 个失败，集合完全一致；witness 必在穷举坏集合内 |
| 互斥路径 | `MUTEX_HOLDS` / `MUTEX_ONE_FAILS` | 两分支各自成立→holds、2 条 safe 路径、穷举 21 个全完成；else 可失败时坏集合恰为 x∈[5,9]、定位到 stmt 2 |
| 整数回绕 | `WRAP_AROUND` (`y=(x+100)%256; assert y>200`) | 手算坏集合 `[0,100]∪[156,255]`（201 个）与穷举一致；witness 重放得到相同回绕值并在 stmt 1 失败；trap 模式下 56..255 报 `overflow` |
| 不可行分支 | `INFEASIBLE_BRANCH`、不可行分支上的除零 | 死分支中的 `assert 0` 不触发；穷举 3 完成 / 253 被 assume 排除；`x==3` 时 `42/(x-3)` 报 `div_by_zero@1(op=udiv)`，且仅此一个输入 |
| 有限循环 | `loop_counter(n)` | n≤10 穷举与引擎一致（11 条路径、最大展开 10）；展开上限 8、n≤30 时判 **unknown** 且带 `loop_unroll` cut；路径预算耗尽判 unknown |

### 独立性保证

* 参考答案不是被测核心自己生成的：
  * **具体解释器**（`se-lang/src/interp.rs`）独立于符号引擎直接遍历 AST；
  * **穷举 oracle**（`se-verify/src/oracle.rs`）只调用具体解释器；
  * **独立暴力求解器**（`se-integration/src/brute.rs`）自带 SMT-LIB
    s-表达式解析与求值，通过枚举域来回答 sat/unsat，驱动引擎再与穷举 oracle
    对照；域超容量时返回 unknown 而不是猜测。
* 独立测试断言**具体结果与失败类别**（具体输入集合、失败位置、op 名称、
  重放步数/存储），不是“接口能调用”。
* 证据防篡改测试：越界值、绑定幽灵变量、张冠李戴的失败类别/位置都会被重放层
  `rejected`，且引擎判 violation 但无确认证据时最终判 `unknown`。

运行全部测试（58 个测试，含真实 Z3 与独立暴力后端两组）：

```bash
cargo test --workspace
cargo clippy --workspace --all-targets   # 零警告
```

---

## 剩余限制（明确边界）

1. **有界分析**：循环按 `max_loop_unroll` 展开、路径按 `max_paths` 截断。
   触界即 `unknown`（带 cut 原因），不声称安全；不支持无界循环的完全判定。
2. **输入域声明**：输入必须给出 `low/high`；符号分析默认把域作为永久约束，
   大域（如整个 u32）虽可解但穷举 oracle 会被 `oracle_cap` 截断为 unknown。
3. **语言范围**：定长无符号/有符号整数算术、位运算、移位、比较、ite、
   赋值、if/while、assume/assert；无数组、堆、函数调用、浮点。
4. **求解器边界**：Z3 的 QF_BV `unknown`/超时/后端缺失一律保守转 unknown；
   每次查询独立起子进程（无增量求解），面向“小程序”规模。
5. **除以零与 `INT_MIN/-1`**：参考语义中除零求值为 0 **并同时**报
   `div_by_zero` 守卫；wrap 模式下回绕是合法结果，trap 模式下为 `overflow`。
6. 服务默认仅监听 `127.0.0.1`，面向本地/合成夹具使用，未内置鉴权与 TLS。
