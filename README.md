# cnf-dpll — DIMACS CNF 求解后端

基于 **DPLL + 双监视文字（two-watched-literal）BCP + 确定性分支 + 1-UIP 子句学习**
的布尔可满足性求解后端，技术栈为 **Rust / Axum / Serde**。全部数据与依赖均为
本地合成夹具，不需要任何外部账号或真实业务数据。

核心特性是**可检验证据（certifying）**：

- `SAT` 返回**完整全赋值模型**，可由独立检查器逐条子句复核；
- `UNSAT` 返回**线性归结（resolution）推导记录**，可由独立检查器逐步验证至空子句；
- 预算耗尽只返回 `UNKNOWN`，**绝不**算作不可满足；
- 检查器与求解器**不共享规范化与证明判定代码**，另有第三份暴力真值表 oracle
  在测试中提供参考答案。

---

## 1. 模块结构（每个模块有真实职责）

```
src/
  lit.rs              变量/文字编码（2v=正文字, 2v+1=负文字, lit^1=互补）
  normalize.rs        子句规范化：去重复文字、删重言子句、保留空子句
  input/mod.rs        DIMACS CNF 输入语言解析器 + 分类解析错误
  solver/
    clause.rs         求解器内部子句（双监视位置、来源）
    budget.rs         时间/传播/决策预算与计数器
    engine.rs         DPLL 内核：BCP、确定性分支、1-UIP 分析、证明生成
    mod.rs            对外门面
  evidence/
    types.rs          SAT 模型 / UNSAT 归结证明的 serde 数据契约
    checker.rs        ★独立检查器：自带规范化与归结规则，不依赖求解器
  diagnostics/mod.rs  请求 id、关键状态、DIMACS 脱敏
  config.rs           环境变量配置
  api/                Axum 路由、DTO、处理器
  main.rs             HTTP 服务（cnf-api）
  bin/cli.rs          命令行（cnf-cli）

tests/
  common/oracle.rs    ★第三份独立实现：暴力 2^n 真值表枚举（参考答案）
  acceptance_core.rs      真值表对账 / 级联传播 / 连续回溯 / 空子句
  acceptance_evidence.rs  篡改模型与证明必须被拒（断言具体失败类别）
  acceptance_budget.rs    预算耗尽=UNKNOWN；解析错误分类
  acceptance_api.rs       HTTP 端到端
  exhaustive_subspace.rs  4096 个公式子空间完全枚举三方对账
fixtures/             本地 DIMACS 夹具（SAT/UNSAT/级联/空子句）
examples/             示例请求体
config/               配置示例
```

## 2. 快速开始

需要 Rust 工具链（在 1.98 上验证）。

```bash
cargo build --release

# 启动 HTTP 服务（默认 127.0.0.1:8080）
./target/release/cnf-api
# 可用环境变量覆盖，见 config/config.example.env
CNF_API_BIND=127.0.0.1:9000 CNF_TIME_LIMIT_MS=2000 ./target/release/cnf-api
```

健康检查：

```bash
curl http://127.0.0.1:8080/health
# {"service":"cnf-dpll","status":"ok"}
```

### 命令行

```bash
./target/release/cnf-cli fixtures/unsat_2vars.cnf        # 退出码 0=SAT/UNSAT 且已自验
./target/release/cnf-cli --time-ms 1 --max-propagations 0 hard.cnf
                                                          # 退出码 3=UNKNOWN
```

## 3. HTTP 接口

### `POST /solve`

接受 DIMACS 正文（`dimacs`）或结构化子句（`clauses` + `num_vars`，二选一）。
可选请求级预算（只能比服务端配置更紧）。

```bash
curl -s -X POST http://127.0.0.1:8080/solve \
  -H 'content-type: application/json' \
  -d @examples/solve_sat.json
```

响应包含 `outcome`（SAT/UNSAT/UNKNOWN 及证据）、`normalize_report`、
`diagnostics`（请求 id、变量/子句数、决策/传播/冲突计数、接受理由）。

- SAT 模型：`outcome.model.true_literals` 为每个变量恰好一个为真的编码文字。
  编码文字 `2v` 表示变量 v 为真，`2v+1` 表示变量 v 为假。
- UNSAT 证明：`outcome.proof.steps[]`，每步 `main` 与 `side[]` 引用
  `{"kind":"input","idx":i}` 或 `{"kind":"lemma","id":k}`，给出 `pivot_vars`
  与归结结果 `resolvent`；最后一步结果必须为空子句。

### `POST /verify/model` 与 `POST /verify/proof`

**独立检查器端点**：给定公式与证据，返回 `MODEL_VALID/INVALID` 或
`PROOF_VALID/INVALID`，并对每个失败给出机器可读的 `failure_category`
（如 `clause_not_satisfied`、`resolvent_mismatch`、`no_pivot`、
`bad_lemma_ref`、`final_not_empty` 等）。

```bash
# 先求解，再把返回的 proof 原样送回验证；篡改后再验证会被拒绝
curl -s -X POST http://127.0.0.1:8080/solve -H 'content-type: application/json' \
  -d '{"dimacs":"p cnf 2 4\n1 2 0\n1 -2 0\n-1 2 0\n-1 -2 0\n"}'
```

解析类错误返回 **422** 与具体 `error_category`（`parse_error`、
`literal_out_of_range`、`missing_num_vars`、`ambiguous_input` 等），
不是笼统的 bad request。

## 4. 关键语义与边界（验收点对应）

| 要求 | 实现 |
|---|---|
| 重复/互补/空子句先规范 | `normalize.rs` 去重、删整条重言、保留空子句；检查器与 oracle 各自独立实现同义规范化 |
| 回溯恢复赋值与传播队列 | `cancel_until` 撤销 value/level/reason 并收缩 `qhead`；双监视指针不回滚（有单测断言） |
| SAT 返回完整可检模型 | 对 1..=n 每变量恰好一个极性；独立检查器逐子句判定 |
| UNSAT 返回可验证推导 | 1-UIP 冲突分析同步落归结步；层 0 归结到空；空/矛盾单位子句有专门证明步 |
| 预算耗尽 → UNKNOWN | 时间/传播/决策三类预算，在传播循环与决策前检查；UNKNOWN 不被检查器背书 |
| 篡改必被拒 | 模型翻转/截断/双极性/重复/越界、证明 resolvent/枢轴/悬空引用/乱序均被独立拒绝并分类 |
| 诊断带 id 与关键状态 | `request_id` 贯穿响应与日志，含计数与结论理由 |
| 敏感数据脱敏 | 日志默认只记结构与计数；需回显输入片段时经 `redact_dimacs`（文字→`#`） |

### 测试不是"接口能调用"

- 断言**具体结果**：级联传播的模型与 0 决策计数、连续回溯的冲突/决策数、
  空子句的单步证明、PHP 的多步归结。
- 参考答案**不来自被测核心**：`tests/common/oracle.rs` 是独立暴力枚举。
- **4096 个公式完全枚举**（`exhaustive_subspace`）+ 500 个 3/4 变量随机公式，
  每个都做 solver ↔ 独立检查器 ↔ oracle 三方对账。

运行测试：

```bash
cargo test
```

## 5. 支持范围与关键取舍

**支持**

- 标准 DIMACS CNF（`c` 注释、`p cnf n m` 头、`0` 终止、子句可跨行、
  容忍文件末尾省略最后一个 `0`）；结构化 JSON 子句输入。
- 双监视文字 BCP（非学习子句的近线性传播）、1-UIP 子句学习与非顺序回溯、
  确定性分支（编号最小未赋值变量，恒取正文字）。
- 线性、可机器检验的 UNSAT 归结证明与 SAT 模型。

**刻意的取舍**

- **确定性优先于性能**：无 VSIDS/相位保存/随机重启，同一公式轨迹逐次可复现，
  便于审计与回归。因此在大型工业实例上不及 Minisat 类求解器；目标是清晰、
  可靠、证据可验，而非竞赛性能。
- **证明格式为线性链**（主句依次与若干边句归结）：足以表达 1-UIP 学习过程，
  但不是 DRAT 那样紧凑的通用证明格式；每步要求唯一互补枢轴，多枢轴显式拒绝。
- 头部声明子句数不一致按**错误**处理（严格模式），不静默纠正。
- 头中变量数小于实际最大变量时报越界错误（DIMACS 严格语义）。
- 预算检查点位于传播外层循环/决策前，极小预算下可能先完成一次初始传播；
  `max_*=0` 可强制在任何搜索前 UNKNOWN。

**未覆盖**

- 非 CNF（如 AIG/DRAT 输入）、预处理（变量消除、有界变量消除）、
  并行求解与增量假设接口（`solve under assumptions`）；这些超出本次范围。

## 6. 依赖锁定

`Cargo.lock` 已提交，所有第三方版本（axum 0.7、tokio、serde、tracing 等）
固定可复现。构建不访问任何生产服务。
