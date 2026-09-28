# mus-service — 可替换 SAT 求解器的子集极小不可满足核提取服务

纯后端服务。给定一组**带唯一身份**的命题 CNF 约束，找出一个
**子集极小（subset-minimal）不可满足核（MUS）**：核整体不可满足，且移除其中
**任意一个**成员后变为可满足。求解内核可按配置/请求替换；结果由**独立的第二套
求解器**重新核验，并为每次删减保留判定依据。

技术栈：Rust · Axum · Serde（Tokio 运行时）。无外部账号、无真实业务数据，
所有输入均为本地合成夹具。

---

## 1. 它保证什么（以及不保证什么）

### 保证

- **每个约束有唯一身份。** 身份由调用方提供（`id`），服务不自造、不合并。
  两条内容完全相同但 `id` 不同的子句被当作两个独立约束追踪。
- **UNKNOWN 永远不等于 UNSAT。** 求解器超时、决策预算耗尽、外部进程无结论等
  一律显式标记为 `unknown`，贯穿提取、核验和 API 三层，从不"猜成不可满足"。
- **输出核满足子集极小定义。** 完成的运行中，每个保留成员都有一次
  「去掉它之后 SAT」的试验及其可满足模型作为证据；独立核验器再做
  `核整体 UNSAT` + `逐成员删除 SAT` 的完整重核，通过才标记 `certified`。
- **取消保留已验证候选及其证明状态。** 已被证明冗余而永久删除的不回滚；
  已 SAT 见证的成员标 `sat_witness`；未尝试的成员如实标 `untested`，
  绝不把"没试过"伪装成"已证明"。
- **可审计的删减依据。** 每个决策记录包含请求标识、候选身份、试验判定
  （sat/unsat/unknown）、动作（kept/removed/kept_undecided）、自然语言依据、
  累计求解次数。

### 明确**不**保证

- **不保证基数最小（cardinality-minimal / SMUS）。** 删减式算法返回**一个**
  子集极小核；当存在多个重叠核时，返回哪个取决于子句顺序。调换顺序可能得到
  另一个同样合法但大小不同的核。测试 `subset_minimality_is_not_cardinality_minimality`
  专门固定这一区分。
- **不是高性能 SAT 求解器。** 内置 DPLL 是为"零依赖可运行 + 确定性"服务的
  教学级实现；生产负载应在配置里接入外部求解器（见下）。
- 核验器（真值表枚举）有变量上限（默认 22）。超限返回 `inconclusive`，
  即"不接受也不拒绝"，而不是信任未核验的核。

---

## 2. 模块结构（真实职责分离，非单文件脚本）

```
src/
├── language/      输入语言：CNF、Literal、ClauseId 身份契约、校验、DIMACS 解析
├── solver/        可替换求解内核
│   ├── mod.rs       SatSolver trait、SolveStatus(sat/unsat/unknown)、预算、取消
│   ├── dpll.rs      内置 DPLL（单元传播，确定性分支顺序）——默认提取求解器
│   ├── brute.rs     真值表枚举——默认"独立"核验器，与 DPLL 零共享搜索代码
│   ├── external.rs  任意外部 DIMACS CLI 求解器适配器（kissat/minisat 风格）
│   └── registry.rs  按配置/名称构造求解器（"可替换"的落点）
├── evidence/      证据核验：整体 UNSAT + 逐成员 SAT，输出 Verdict 与证人模型
├── core/          删减式 MUS 提取状态机：预算、取消、UNKNOWN、证明状态、审计
├── diag/          决策记录与脱敏（敏感约束只出现 id + 指纹，不出现文字量）
├── config/        config.toml + 环境变量
├── api/           Axum：同步 /extract 与可取消的异步 /jobs
├── lib.rs         模块导出与状态装配
└── main.rs        进程入口

tests/
├── common/
│   ├── oracle.rs      ★ 第三套独立 SAT 实现（仅供测试，不与被测代码共享任何搜索逻辑）
│   └── fixtures.rs    手工夹具（重叠核/冗余/同义反复/重复身份）+ 手写预期答案
├── extraction_core.rs        算法层：具体核、具体失败类别、oracle 复核
├── evidence_independence.rs  证据层：certified / not_minimal / inconclusive / 拒绝
└── api_http.rs               接口层：具体载荷、错误类别、取消、脱敏
```

**参考答案不是被测实现自己生成的**：测试的预期核是夹具里手写的 id 集合，
SAT/UNSAT 与极小性再由 `tests/common/oracle.rs` 中第三套独立实现
（`Vec<Vec<i64>>` + 全新赋值数组 + 从头评估每条子句的回溯，无单元传播）复算。

---

## 3. 本地启动

前置：Rust 工具链（开发环境为 1.98）。

```bash
# 1. 构建（首次会拉取并锁定依赖）
cargo build --release

# 2. 直接运行（零配置，默认监听 127.0.0.1:8080）
cargo run

# 或使用配置文件
cp config.example.toml config.toml
MUS_CONFIG=config.toml cargo run
```

健康检查：

```bash
curl -s http://127.0.0.1:8080/healthz | jq
```

返回默认提取求解器、独立核验器和当前已注册求解器清单。

### 接入外部求解器（可选）

在 `config.toml` 注册一个输出标准 `s SATISFIABLE / s UNSATISFIABLE` 的二进制：

```toml
[[solvers]]
kind = "external"
name = "kissat"
argv = ["kissat", "{in}"]
```

然后在请求体里传 `"solver": "kissat"`，或设 `MUS_DEFAULT_SOLVER=kissat`。
二进制缺失、超时、输出无法解析都会得到 `unknown`，不会崩溃也不会误判。

---

## 4. API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/healthz` | 存活 + 求解器清单 |
| POST | `/extract` | 同步运行，返回完整报告 |
| POST | `/jobs` | 创建异步任务 |
| GET  | `/jobs/:id` | 轮询生命周期 / 取报告 |
| POST | `/jobs/:id/cancel` | 请求取消（保留已验证候选） |

所有响应回显 `x-request-id`（可在请求头自定义，便于关联日志）。

### 请求字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `clauses` | 数组 | 每项 `{id, literals:[...DIMACS 整数], sensitive?}` |
| `solver` | 字符串? | 命名求解器，缺省用服务默认 |
| `verify` | 布尔? | 是否独立核验（默认 true） |
| `order` | 枚举? | `input`（默认）/`shortest_first`/`longest_first` |
| `max_solver_calls` | 整数? | 总求解调用预算（含首次可满足性检查） |
| `max_decisions` | 整数? | 单次调用的分支决策预算 |

### 示例：同步提取（重叠核 + 冗余）

```bash
curl -s -X POST http://127.0.0.1:8080/extract \
  -H 'content-type: application/json' \
  -H 'x-request-id: demo-1' \
  --data @examples/overlapping.json | jq
```

响应（节选）：

```json
{
  "request_id": "demo-1",
  "outcome": "completed",
  "core": ["d", "e", "r_dup_c", "r_weak"],
  "member_proofs": [ {"id":"d","state":"sat_witness"}, ... ],
  "verification": { "verdict": "certified", "verifier_solver": "brute", ... },
  "decisions": [
    {"seq":0,"candidate":{"id":"a","content":{"mode":"plain","literals":[1]}},
     "trial_status":"unsat","action":"removed",
     "basis":"trial of (candidate minus clause) = UNSAT: clause redundant, permanently removed"}
  ],
  "solver_calls": 10
}
```

注意这个结果是删减式算法在**该输入顺序**下的正确产物：删去 `a` 后核 B 仍
UNSAT，且原本被 `a` 吸纳的 `r_weak` 升级为必要约束。该核仍满足子集极小性
（由独立核验器与测试 oracle 双重确认）。**`r_dup_c` 与 `c` 内容相同但身份
不同，因此不被合并。**

### 示例：预算超限（明确的失败类别，而非崩溃或误判）

```bash
curl -s -X POST http://127.0.0.1:8080/extract \
  -H 'content-type: application/json' --data @examples/budget.json | jq '.outcome'
# "budget_exhausted"
```

报告里 `solver_calls == 3 == budget_limit`，未尝试成员标 `untested`，
`verification` 缺省并附带 `verification_skipped_reason`（"requires a complete
run"）。

### 示例：可取消的异步任务

```bash
JOB=$(curl -s -X POST http://127.0.0.1:8080/jobs \
  -H 'content-type: application/json' --data @examples/overlapping.json \
  | jq -r .job_id)

curl -s -X POST http://127.0.0.1:8080/jobs/$JOB/cancel
curl -s http://127.0.0.1:8080/jobs/$JOB | jq '.lifecycle, .report.outcome'
```

### 示例：敏感数据脱敏

```bash
curl -s -X POST http://127.0.0.1:8080/extract \
  -H 'content-type: application/json' --data @examples/sensitive.json | jq
```

标记 `sensitive` 的子句，决策记录中其内容显示为

```json
{"id":"secret-alpha","content":{"mode":"redacted","formula_fingerprint":"…"}}
```

文字量（1111/-1111）不出现在响应或日志中；id 作为关联键保留。

### 错误类别（稳定、可机读）

`malformed_json` · `invalid_input`（含重复 id / 空 id / 非法文字量）·
`unknown_solver`（附带可用清单）· `job_not_found` · `job_not_cancellable` ·
`method_not_allowed` · `internal`。SAT 输入与 UNKNOWN 初检不是 HTTP 错误，
而是 200 报告中的 `outcome: "input_sat" / "input_unknown"`。

---

## 5. 算法与关键取舍

删减式（deletion-based）MUS：

```
对整个输入求解一次：
    SAT     -> 无核（input_sat）
    UNKNOWN -> 不报核（input_unknown），绝不当作 UNSAT
    UNSAT   -> 继续
候选 := 全部子句
按所选顺序逐个尝试删除子句 c：
    solve(候选 - c) = UNSAT   -> 矛盾在没有 c 时仍成立：c 冗余，永久删除
                       SAT   -> 矛盾需要 c：保留，记录可满足模型为证
                   UNKNOWN   -> 无法判定：保守保留并标 unknown_trial
完成后（可选）独立核验器重核极小性
```

取舍：

- **删减式 vs 插入式**：删减式从全量出发，天然适合预算/取消时返回"部分但
  诚实"的候选；代价是调用次数为 O(n)。
- **UNKNOWN 保守保留**：宁可把无法判定的成员留在核里（核可能不够小），也
  绝不删除可能必要的约束——保证返回的候选在已知证据内不被污染。
- **核验器与提取器必须是两套实现**：系统给自己发"合格证书"不构成独立证据。
- **确定性**：内置求解器分支顺序固定（最小未赋值变量、先试真），相同输入
  可复现。

---

## 6. 运行测试

```bash
cargo test
```

测试不是"接口能调通"级别的，而是断言**具体结果与具体失败类别**：

- 多个重叠核、四类冗余（无关单元 / 内容重复 / 同义反复 / 被吸纳弱约束）下
  的精确核 id 集合，并用独立 oracle 重证 `整体 UNSAT + 逐成员 SAT`；
- 基数 vs 子集极小的顺序对照；
- 预算超限是独立 outcome，且不发核验证书；
- UNKNOWN 初检（不报核）与 UNKNOWN 中途试验（保守保留 + 降级 outcome）；
- 取消在"初检后/若干试验后"两个时点的证明状态区分；
- 核验器自身 UNKNOWN 时结论为 `inconclusive` 而非 `certified`；
- 重复 id、空 id 等输入身份契约错误；
- 敏感文字量在载荷中不可推导。

---

## 7. 已知限制 / 未实现项

- 作业状态保存在内存 `HashMap`，进程重启即丢失（无持久化，定位为本地服务）。
- 作业 TTL 字段已在配置中，过期清理任务**尚未接线**（字段当前不生效）。
- 外部求解器只解析 `s SATISFIABLE/UNSATISFIABLE` 与可选 `v` 模型行；不支持
  返回 DRAT 不可满足证明的校验（DRAT 证明核验超出当前范围）。
- 无鉴权/TLS：默认绑定回环地址，定位为本地后端。
