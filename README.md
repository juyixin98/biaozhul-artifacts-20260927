# DIMACS CNF 求解后端

一个自包含的布尔可满足性（SAT）求解服务：输入 DIMACS CNF（或结构化 JSON），
内核用 **DPLL + 双监视文字（two-watched literals）+ 1-UIP 冲突分析/学习 +
确定性静态分支**；SAT 返回**完整可检验模型**，UNSAT 返回**可独立重放的线性消解推导**，
搜索/时间预算耗尽返回 **UNKNOWN**（绝不会冒充 UNSAT）。

技术栈：Rust · Axum · Serde · Tokio · Tracing。除 crates.io 依赖外没有任何外部服务、
账号或业务数据；`fixtures/` 提供全部本地合成夹具。

---

## 1. 目录结构与模块职责

| 路径 | 职责 |
| --- | --- |
| `src/cnf.rs` | CNF 数据模型；**子句规范化**：排序去重、互补对（重言式）整条丢弃、显式保留空子句 |
| `src/input.rs` | 输入语言：DIMACS 文本解析器（注释、`p cnf` 行、跨行子句、EOF 自动闭合）与 JSON 入口 |
| `src/solver.rs` | **求解内核**：trail + 传播队列、双监视文字传播、1-UIP 冲突分析与子句学习、分层回溯、消解证明记录、预算控制 |
| `src/evidence.rs` | 证据契约类型：`ResolutionProof` / `DerivedClause` / `ResolventOp`（Serde 序列化） |
| `src/verify.rs` | **独立检查器**：不导入 `solver`；模型逐子句判定，证明逐步重放消解并核对声明结果 |
| `src/api.rs` | Axum 路由、DTO、统一错误体、请求 id、脱敏诊断日志 |
| `src/config.rs` | 环境变量配置与校验 |
| `tests/` | 独立集成测试：穷举真值表预言机、证据篡改拒绝、API 端到端、大规模穷举 |
| `fixtures/` | 本地合成 DIMACS 夹具（级联传播 / 连续回溯 / 空子句 / UNSAT） |
| `examples/` | CLI 示例、curl 冒烟脚本、请求样例 |
| `config/local.env.example` | 本地配置模板 |

### 关键不变量

- **规范化先行**：重复文字去重、含互补对的重言子句丢弃、空子句保留（直接定死 UNSAT）。
  规范化结果对输入顺序不敏感，并以 `normalization` 字段回报每一处改动。
- **回溯恢复赋值与传播队列**：所有赋值在单条 `trail` 上，`trail_lim` 记录各决策层起点；
  回溯弹出尾部赋值、清空 `value/reason/level`，并把传播队列游标 `qhead` 复位为
  当前 trail 长度——旧分支的待传播文字不会泄漏到新分支。
- **预算耗尽是 UNKNOWN**：决策预算与墙钟预算只在“准备做下一次分叉”时检查；
  纯单位传播能得到的结论（含第 0 层 UNSAT）不被预算截断。
- **内核不能自证**：`/solve` 返回前，模型/证明必须通过 `verify` 独立检查器；
  检查失败时对外降级为 UNKNOWN 并记录告警，绝不输出伪结论。

## 2. 证据格式

**模型**：DIMACS 风格有符号文字数组，每个变量恰好一项，`v` 表示真、`-v` 表示假。

**消解证明**（只引用输入子句或同份证明中先出现的派生条目）：

```jsonc
{
  "derived_clauses": [
    {
      "id": "d0",
      "literals": [1],                 // 声明的消解结果（规范化排序）
      "start_ref": "i1",              // 起始子句：i<n> 输入子句序号
      "resolvents": [
        { "pivot_var": 2, "with_ref": "i0" }  // 在变量 2 上与 i0 消解（极性必须相反）
      ]
    }
  ],
  "empty_clause_ref": "d0"            // 最终必须指向一条空子句
}
```

检查器逐步重放：枢轴变量在两侧各出现恰好一次且极性相反、消解结果与 `literals`
逐字一致、引用不存在/前向引用/参与消解的空子句/非空 final 引用等都会按**具体类别**拒绝。

## 3. 本地启动

```bash
cargo test                      # 全部测试（见第 6 节）
cargo run                       # 默认监听 127.0.0.1:8080
```

可用环境变量（见 `config/local.env.example`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CNF_HTTP_BIND` | `127.0.0.1:8080` | 监听地址（默认只绑回环） |
| `CNF_MAX_DECISIONS` | `100000` | 每请求决策预算；`0` 只允许传播；`-1` 不限 |
| `CNF_TIME_LIMIT_MS` | `1000` | 每请求墙钟预算；`-1` 不限 |
| `CNF_LOG_FORMAT` | `text` | `text` / `json` |

日志只记录 request_id、变量/子句规模、决策数、冲突数、结论等元数据，
**不记录公式内容**——当输入被视为敏感数据时不会出现在日志里。

## 4. HTTP 接口

### `GET /healthz` → `200 {"status":"ok"}`

### `POST /solve`

请求（结构化子句与 DIMACS 二选一；`num_vars` 省略时自动推断）：

```bash
curl -sS localhost:8080/solve -H 'content-type: application/json' -d '{
  "request_id": "demo",
  "num_vars": 2,
  "clauses": [[1, 2], [-1], [-2]]
}'
```

DIMACS 形式：

```bash
curl -sS localhost:8080/solve -H 'content-type: application/json' \
  --data @examples/requests/unsat_dimacs.json
```

预算覆盖（省略字段回退服务端配置；`-1` 显式取消限制）：

```json
{ "clauses": [[1,2],[1,-2]], "limits": { "max_decisions": 100, "time_limit_ms": 50 } }
```

响应统一带 `request_id`（可自带，否则生成 UUID）、`verdict`（`sat|unsat|unknown`）、
人类可读 `conclusion`、`evidence_check`（`accepted|rejected` + 具体错误类别）、
`diagnostics`（决策数/冲突数/监视检查次数/停止原因等关键状态）。

错误：畸形 JSON / 未知字段 / 变量越界 / 文字 0 等一律 `400`，
带 `category`（`malformed_json` / `invalid_input`）与 request_id。

### `POST /verify`

对**任意外部证据**（可能已被篡改）做独立复核：

```bash
curl -sS localhost:8080/verify -H 'content-type: application/json' \
  --data @examples/requests/verify_tampered_model.json
# {"accepted": false, "error": {"kind":"clause_unsatisfied","clause_index":1}, ...}
```

### 一键冒烟

```bash
./examples/curl_smoke.sh        # 自动起服务，发 SAT/UNSAT/UNKNOWN/篡改 四类请求
cargo run --example solve_file -- fixtures/unsat_pigeonhole_small.cnf
```

## 5. 支持范围与关键取舍

**支持**

- 标准 DIMACS CNF：`c` 注释、`p cnf n m`（子句数校验，可省略）、子句跨行/共行、
  EOF 未写 `0` 自动闭合、`%` EOF 标记；结构化 JSON 等价入口。
- DPLL 全套：双监视文字单位传播、1-UIP 子句学习、按学习子句回溯层非顺序回退、
  第 0 层冲突直接消解出空、显式空子句短路。
- 可检验证据与独立检查器（模型与消解证明两条路径）。
- 决策/时间双预算、UNKNOWN 语义、确定性（同输入同预算 ⇒ 同搜索路径同证明）。

**刻意不做（取舍）**

- 不做子句删除/重启/VSIDS：分支用确定性静态次序（文字出现次数降序、变量号升序，
  固定负极性），换取可复现与简单可审计；规模很大的硬实例不是本后端目标。
- 消解证明是“线性、可逐步重放”的格式，不是 DRAT/LRAT 这类紧凑工业格式；
  证据可能随学习过程线性增长，换取检查器极简、无核化、易独立审计。
- 单请求同步求解（Tokio 阻塞任务之外的 CPU 工作未额外 spawn blocking）；
  默认预算（10 万决策 / 1 秒）保证单请求占用有界。
- 没有鉴权/TLS/持久化：本地回环服务定位，部署到共享网络需自行加边车。

## 6. 测试策略（参考答案不由被测核心生成）

- **穷举真值表预言机**（`tests/common/oracle.rs`）：独立实现，枚举全部 2^n 赋值，
  是判定 ground truth 的唯一来源。
- `tests/solver_oracle.rs`：全部 2 变量二元子句组合（100 例）、
  3 变量 400 个确定性抽样子集，逐一要求求解器结论与预言机一致，
  且 SAT 模型/UNSAT 证明通过独立检查器；另覆盖级联传播（断言 `decisions == 0`）、
  矛盾空子句、预算 UNKNOWN。
- `tests/exhaustive.rs`：**全部 3 变量、≤3 条子句公式（约 2 万例）** +
  4 变量 600 个 LCG 抽样公式，全部与真值表交叉核对并验证证据。
- **篡改拒绝**：翻转模型位、翻转派生文字、偷换空子句引用、伪造子句引用、
  同极性枢轴、声明结果与重放不符——测试断言到**具体失败类别**
  （`clause_unsatisfied` / `incomplete` / `bad_pivot` / `resolvent_mismatch` /
  `empty_ref_not_empty` / `unknown_ref` 等），不是“接口能调通”。
- `tests/api.rs`：内存路由端到端断言状态码、verdict、request_id 关联、
  预算语义、规范化诊断、400 类别。

```bash
cargo test            # 46 个测试：24 单元 + 22 集成
```

## 7. 已知限制 / 未执行项

- 未在 Windows 上验证（开发与 CI 目标为 Linux/macOS）。
- 未做属性化（proptest）模糊测试；穷举+确定性抽样已固定，扩展到 5+ 变量时
  建议引入 fuzz target。
- 证明体积无上限裁剪；长搜索的 UNSAT 响应可能较大（受决策预算间接约束）。
