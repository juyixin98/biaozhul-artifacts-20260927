# Local Explicit-State FSM Model Checker

一个完全本地的有限状态机**显式状态模型检查器**，支持 **AG 安全性（不变量）** 与
**EF 可达性** 子集。Rust + Axum + Serde，无任何外部业务依赖或真实账号；
所有数据都是随仓库提供的本地合成夹具。

---

## 1. 它做什么

输入是一份**声明式状态规范**（JSON），声明：

- **状态变量及有限域**：`int_range {lo,hi}`（含端点）或 `bool`；
- **初始状态谓词** `initial`：满足它的全部域元素都是初态（必须至少一个）；
- **转移** `transitions[]`：每个转移有 `guard`（守卫）和 `updates`（同一前态的
  同时更新，见 §3）；
- 可选 **合法终止谓词** `terminal`：与死锁严格区分；
- **性质** `properties[]`：`ag`（所有可达状态满足）或 `ef`（某可达状态满足）。

内核从初态集合出发做 **BFS**，给出：

- 每条性质的判定：`true / false / unknown`；
- **最短**反例（AG）/ 见证（EF）路径，逐步列出状态与触发的转移；
- **死锁** 与 **合法终止** 两类不同结构证据；
- 探索统计（发现/展开状态数、守卫求值数、转移触发数、死锁/终止数、域总大小）；
- 超过预算时给 **unknown + 统计**，**绝不**因“没找到反例”而宣称证明。

每条返回的证据都会由一个**独立 crate（`fsm-evidence`）重放校验**；该重放器
不包含也不调用内核 BFS，只依据规范与序列化证据逐步重算，因此内核不能用自身
逻辑为自己的输出背书。

---

## 2. 模块关系

```
crates/
  fsm-lang/      输入语言：规范模型(Spec)、名字/类型解析与编译(CompiledSpec)、
                 表达式语义、有限域枚举、同时更新、规范指纹(SHA-256)、证据结构
  fsm-core/      求解内核：去重 BFS、预算控制、AG/EF 判定、最短路径、死锁/终止、统计
  fsm-evidence/  独立证据验证：逐步重放（初态?守卫?同时更新精确再现?终态主张?）
  fsm-server/    后端接口(Axum) + CLI + 配置；串联上面三者并做请求关联/可解释输出

fixtures/       本地合成夹具（互斥协议、受限计数器、死锁、无初态、截断链、
                 越域、溢出、同时交换）
config/         独立 TOML 配置样例
```

依赖方向：`fsm-server → {fsm-core, fsm-evidence, fsm-lang}`，
`fsm-evidence → fsm-lang`（**不**依赖 `fsm-core`），`fsm-core → fsm-lang`。
内核与验证器共享的只有 `fsm-lang` 里的规范/证据数据契约。

---

## 3. 关键语义约束

### 3.1 变量域与更新
- 更新表达式**只在更新前的状态上求值**：一个转移内所有 RHS 先全部求值，再统一
  写回。因此 `a:=b; b:=a` 是真正的交换（见 `fixtures/swap.json`）。
- 更新结果若落在声明域之外，报告 `VALUE_OUT_OF_DOMAIN` 模型错误，**不会**静默
  截断或与别的状态混淆。
- 整数算术用 `checked_*`，溢出报 `INT_OVERFLOW`（不回绕）。

### 3.2 去重与最短反例
- 状态用完整赋值（按声明顺序的 `Value` 向量）结构去重。
- BFS + 转移按声明顺序考虑，保证**第一条**反例/见证是**最短**路径且确定性。
- 每条规范另有 SHA-256 指纹（键排序的规范 JSON），用于在结果/日志中标识“被检查
  的到底是哪一份规范”。键顺序与空白变化不改变指纹。

### 3.3 死锁 ≠ 合法终止
- **合法终止**：状态满足可选 `terminal` 谓词；该状态不再展开，记为终止并给路径。
- **死锁**：状态**不**满足终止谓词，且**没有任何**转移守卫成立。
- 二者各自独立计数并产出独立类型证据（`deadlock` / `terminal`）。

### 3.4 预算与“未知”
预算包含三项：`max_states`（展开状态数）、`max_transitions`（守卫求值数）、
`max_initial_scan`（初态扫描的域赋值数）。

- 预算耗尽且**确有剩余工作**（队列非空/当前状态还有未评估守卫/域未扫完）→
  `status=truncated`，所有未被具体反例/见证命中的性质判 **unknown**，原因
  `BUDGET_TRUNCATED`（初态扫描阶段为 `INITIAL_SCAN_TRUNCATED`），并返回统计。
- 预算**恰好**覆盖整个状态图（队列恰空）不算截断。
- 无初态是独立失败类别 `NO_INITIAL_STATE`（`status=invalid`），不会被当成“性质成立”。

---

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活检查 |
| GET | `/version` | 引擎版本、算法、支持逻辑 |
| POST | `/check` | 提交 `{"spec": ..., "budget"?: {...}}`，返回完整判定 |
| POST | `/evidence/replay` | 提交 `{"spec":..., "evidence":...}`，只做独立重放 |

**请求关联**：读取请求头 `x-request-id` 并在响应头与响应体中原样回显；未提供则
本地生成 `local-<nanos>-<n>`。日志以该 id 关联请求的接收、探索开始（含指纹、预算）
与结束（含状态、截断标记）。

**可解释输出**（`/check`）：`request_id`、`engine_version`、`algorithm`、
`spec_fingerprint`、`status/reason/truncated`、`stats`、每条性质的
`verdict/reason/evidence`、`deadlocks`、`terminals`，以及对每条证据的
`evidence_checks`（独立重放是否通过、步数、失败类别）。**失败**（`failure` 块）
与**不确定结论**（`verdict=unknown`）分别呈现，互不混淆。

失败状态码：请求/规范本身错误返回 HTTP 400（`INVALID_JSON` / `INVALID_SPEC` /
`TYPE_MISMATCH` / `UNKNOWN_VARIABLE` 等）；模型可检查但出现运行期问题时 HTTP 200
且在体内用 `status=invalid|error|truncated` 与 `failure` 表达。

---

## 5. 本地验证命令

> 全部离线运行。首次构建需要 crates.io 拉取依赖（serde / axum / tokio 等）。

```bash
# 0) 编译
cargo build

# 1) 全部测试（41 个）：语言 9 + 内核 10 + 独立重放 12 + HTTP 端到端 10
cargo test

# 2) 静态检查（应无 warning）
cargo clippy --all-targets

# 3) CLI 单次检查（打印与 POST /check 同构的 JSON；退出码见下）
cargo run -- check fixtures/mutex_bad.json          # AG 被违反 + 最短反例
cargo run -- check fixtures/counter.json            # 证明/见证/不可达 + 合法终止
cargo run -- check fixtures/no_initial.json         # 退出码 2，NO_INITIAL_STATE
cargo run -- check fixtures/out_of_domain.json      # 退出码 3，VALUE_OUT_OF_DOMAIN
cargo run -- check fixtures/truncate_chain.json \
  --max_states 3                                    # 退出码 4，unknown + 统计
echo "exit=$?"

# 4) 起 HTTP 服务
cargo run -- serve --port 8080
#   另一终端：
curl -s http://127.0.0.1:8080/version
curl -s -H 'x-request-id: demo-1' -H 'Content-Type: application/json' \
  -d "{\"spec\": $(cat fixtures/mutex_bad.json)}" \
  http://127.0.0.1:8080/check
```

CLI 退出码：`0` 完整完成；`2` 无初态/无效；`3` 求值错误；`4` 预算截断；`1` 其他 I/O。

配置（可选）：`config/default.toml`，或环境变量 `FSM_HOST/FSM_PORT/FSM_MAX_STATES/
FSM_MAX_TRANSITIONS/FSM_MAX_INITIAL_SCAN`，或 CLI 参数；优先级 CLI > 环境 > TOML > 默认。

### 预期判断方式
- `mutex_bad`：`mutex => false / COUNTEREXAMPLE_FOUND`，证据 5 个状态、4 次转移
  `request0,request1,enter0,enter1`，末态 `in_cs0=in_cs1=true`；`evidence_checks`
  全部 `replay_valid=true`。
- `counter`：`nonnegative=true/PROVED`，`can_reach_top=true/WITNESS_FOUND`
  （`inc×3`），`can_overflow=false/TARGET_UNREACHABLE`；1 个合法终止、0 死锁。
- `deadlock`：1 死锁（`shoot` 后无可用转移）、0 终止；把该证据类型改成 `terminal`
  重放必失败（`FINAL_NOT_TERMINAL`）。
- `no_initial`：`status=invalid, reason=NO_INITIAL_STATE`，性质列表为空。
- 截断链 `--max_states 3/4`：`status=truncated`，性质全 `unknown` 且无证据，
  统计显示已展开/已发现数量；预算给足 10 则 `complete`。

---

## 6. 测试如何满足“具体断言 + 独立参考答案”

- **断言具体结果与失败类别**：例如精确比较反例触发序列、末态变量值、状态总数、
  `reason` 代码、`failure.code/transition`，而非仅“接口可调”。
- **手工回放每一步**：`kernel.rs` 对互斥反例逐状态断言中间赋值；`fsm-evidence`
  的重放报告逐步给出“守卫成立；转移精确再现该状态”。
- **参考答案不来自被测内核**：
  - `tests/common/independent_oracle.rs` 用一套**独立的递归 DFS** 重算可达集合，
    与内核 BFS 的 9 个可达状态交叉核对；
  - 多条证据是**手工构造**的（如 swap 见证），不是从内核输出提取；
  - 故意篡改证据（改末态、伪造转移名、守卫为假、非初态根、越域值、空轨迹、
    伪造性质名、把终止冒充死锁）后断言**具体失败码**。

---

## 7. 算法假设与限制

- **有限域**：变量域有限且在编译期可知；初态通过**枚举整个笛卡尔积**筛选。域极大
  时由 `max_initial_scan` 兜底并报 `INITIAL_SCAN_TRUNCATED`，不假装完整。
- **逻辑子集**：仅 AG（不变量）与 EF（可达性），不支持嵌套时序算子/AU/EG 等；
  EF 不满足在完整探索下等价于“目标不可达”，截断时仍为 unknown。
- **无公平性建模**：BFS 描述可达性，不做公平路径假设。
- **顺序化探索**：当前为单线程 BFS；预算是正确性边界而非性能优化。
- 整数为 `i64`，全程 `checked_*`，越界即模型错误。

---

## 8. 依赖版本

Rust edition 2021（工具链 1.98 验证通过；`rust-version = 1.80`）。主要依赖：

| crate | 版本 | 用途 |
|---|---|---|
| axum | 0.8 | HTTP 路由/中间件 |
| tokio | 1 | 异步运行时 |
| serde / serde_json | 1.0 | 规范与结果（反）序列化 |
| sha2 | 0.10 | 规范 SHA-256 指纹 |
| hex | 0.4 | 指纹十六进制 |
| tracing / tracing-subscriber | 0.1 / 0.3 | 结构化日志 |
| clap | 4 | CLI |
| toml | 0.8 | 配置文件 |
| tower | 0.5 (dev) | HTTP 端到端测试 |

精确锁定版本见 `Cargo.lock`。
