# petri-reach

**显式容量上界的普通带权 Petri 网标识可达性分析后端。**

技术栈：Rust + Axum 0.7 + Serde（tokio / tracing / tower-http）。所有输入均为本地合成夹具，
不依赖任何生产账号或外部业务数据。

---

## 1. 范围与语义边界（务必先读）

本系统只处理**每个库所都带显式有限容量上界 `capacity` 的普通带权 Petri 网**：

- 令牌是无差别的（非着色网），弧权为正整数；
- 合法标识恒满足 `0 ≤ tokens(p) ≤ capacity(p)`；
- 容量使状态空间有限，大小至多为 `Π_p (capacity(p)+1)`。

行为契约：

1. **变迁启用同时检查全部输入弧**；任一库所令牌不足即禁用，并报告*全部*缺口。
2. **发射是原子的消耗与生成**；先按净增量检查容量，**溢出即禁止发射，绝不截断令牌**。
3. 可达时返回**变迁名序列 + 逐步标识轨迹**，并提供 **P 不变量候选**与其守恒验证。
4. 判定三值：`reachable` / `unreachable` / `inconclusive`。

> **完备性边界（契约 4）**：`unreachable` 仅在**容量模型内**成立——它表示目标不在
> 容量所允许的有限状态空间中。该结论**不等价于无界 Petri 网的完整可达性判定**：
> 无界网可能经由容量模型之外的执行路径到达目标。当 BFS 触达 `state_limit` 被截断时，
> 系统返回 `inconclusive` 而非谎称“不可达”。每个响应都带 `scope` 字段原样声明此边界。

---

## 2. 工程组织

```
src/
  input/      输入语言层：JSON 网描述 与 .pnet 文本语言，统一语义校验与错误码
    json.rs   JSON DTO（deny_unknown_fields）
    pnet.rs   .pnet 词法/语法分析（带行列号诊断）
  kernel/     求解内核（不依赖 HTTP/序列化之外的东西）
    model.rs  网与标识模型
    fire.rs   启用判定（全输入弧）+ 原子发射 + 容量溢出阻断
    invariants.rs  关联矩阵、有理零空间、本原整数基、有界非负 P 不变量枚举
    reach.rs  正向 BFS、不变量快速否决、路径回溯、三值判定
  verify/     证据验证（独立重放，不调用内核 fire；独立核对守恒残差）
  api/        Axum 接口：路由、中间件、DTO、错误映射、处理器
  config/     TOML + PETRI_* 环境变量配置
  request_id.rs 请求关联标识（X-Request-Id 或 UUIDv4，贯穿日志/响应）
tests/
  common/     独立参考实现（递归穷举可达集 + 独立 BFS 最短路），不调用被测求解函数
  fixtures_models.rs          三个夹具网的具体断言
  exhaustive_cross_check.rs   小标识空间全量穷举对照
  api.rs                      HTTP 端到端（判定、失败类别、证据、request-id、体积限制）
fixtures/     mutex.json / producer_consumer.json / deadlock.json / mutex.pnet
requests/     可直接 curl 的请求样例
config/       default.toml（local.toml 可覆盖）
scripts/      build_request.sh 从夹具拼请求体
```

这不是单文件或调用壳：发射语义、求解、证据验证、接口各自独立分层并有独立测试。

---

## 3. 从干净目录复现

需要 Rust 工具链（开发与验证使用 **rustc/cargo 1.98.1**；`Cargo.toml` 声明
`rust-version = "1.75"`）。依赖在首次构建时从 crates.io 拉取并生成 `Cargo.lock`。

```bash
# 1) 构建
cargo build --release

# 2) 跑全部测试（单元 + 集成 + HTTP 端到端）
cargo test

# 3) 静态检查
cargo clippy --all-targets

# 4) 启动（默认 127.0.0.1:8080）
./target/release/petri-reach
#    可选覆盖：
#   PETRI_SERVER_PORT=8099 PETRI_LOG_LEVEL=debug ./target/release/petri-reach
```

配置见 `config/default.toml`；可用 `config/local.toml` 或 `PETRI_*` 环境变量覆盖
（`PETRI_SERVER_HOST/PORT`、`PETRI_SOLVER_STATE_LIMIT`、
`PETRI_INVARIANT_COEFFICIENT_BOUND`、`PETRI_BODY_LIMIT_BYTES`、`PETRI_LOG_LEVEL`）。

> 日志：基准级别取配置 `log_level`，但环境变量 **`RUST_LOG` 若设置则优先**（tracing 约定）。
> 某些环境预置了 `RUST_LOG=warn`，想看请求级 info/debug 日志请显式 `RUST_LOG=info` 或 `debug`。

---

## 4. HTTP 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET  | `/health` | 服务与版本 |
| POST | `/api/v1/reachability` | 可达性分析（判定 + 路径证据 / 不变量否决 / inconclusive）|
| POST | `/api/v1/invariants` | 关联矩阵秩、零空间维数、P 不变量候选 |
| POST | `/api/v1/verify/firing` | 独立重放发射序列，校验证据，可比对声称终点 |
| POST | `/api/v1/verify/invariant` | 独立核对候选权向量守恒残差与加权和 |

所有请求/响应为 JSON。可带 `X-Request-Id` 头关联日志；缺省时生成 UUIDv4，
并在响应头与响应体 `request_id` 中回显。

### 4.1 可达性（可达，返回路径）

```bash
curl -s http://127.0.0.1:8080/api/v1/reachability \
  -H 'content-type: application/json' \
  --data @requests/reachability_mutex_reachable.json | jq
```

关键响应字段：`decision`、`reachable`、`basis`、`certificate.transition_sequence`、
`certificate.marking_trace`、`states_visited`、`state_space_upper_bound`、
`state_limit`、`progress`、`scope`。

### 4.2 不可达（P 不变量守恒否决）

```bash
curl -s http://127.0.0.1:8080/api/v1/reachability \
  -H 'content-type: application/json' \
  --data @requests/reachability_mutex_both_unreachable.json | jq
```

`basis=p_invariant_conservation_witness`，并给出见证权向量与初/目标加权和。

### 4.3 证据验证（独立重放）

```bash
# 合法证据
curl -s http://127.0.0.1:8080/api/v1/verify/firing \
  -H 'content-type: application/json' --data @requests/verify_firing_valid.json | jq
# 非法发射（第 2 步缺 free）-> 422，details 给出 deficits 类别
curl -s http://127.0.0.1:8080/api/v1/verify/firing \
  -H 'content-type: application/json' --data @requests/verify_firing_illegal.json | jq
```

`.pnet` 文本格式可用 `net_text` 字段提交（与 `net` JSON 二选一），见 `fixtures/mutex.pnet`。

### 4.4 失败不是成功：HTTP 语义

- 业务判定（可达/不可达/不确定）一律 **200**，用 `decision` 区分；
- 请求结构或网非法 **400**，`error` 为稳定错误码（见下表），`issues[]` 给全部问题；
- 证据不成立（非法发射步 / 声称终点不符）**422**，`details` 给失败类别与位置；
- 请求体超限 **413**；内部错误 **500**。异常绝不包装成成功。

稳定错误码：`invalid_json`、`net_empty`、`duplicate_place`、`duplicate_transition`、
`unknown_place`、`duplicate_arc`、`nonpositive_weight`、`negative_capacity`、
`negative_tokens`、`token_exceeds_capacity`、`marking_length`、`bad_state_limit` 等。

---

## 5. 三个夹具网

- **互斥资源** `fixtures/mutex.json`：两进程争单资源。单进程入临界区可达；
  两进程同时占用被资源守恒律 `free+crit_a+crit_b=1` 否决。
- **生产消费** `fixtures/producer_consumer.json`：+2 生产 / −3 消费，buffer 容量 5。
  可达 buffer 层恰为 `0..=5`；在 buffer=4 再 produce 会得 6，**容量阻断且不截断**。
- **死锁网** `fixtures/deadlock.json`：交叉加两把锁。`(Wa,Wb)=(1,1)` 可达但零使能变迁，
  从该标识回不到初标识。

---

## 6. 验证材料与独立性

- **每次发射合法**：穷举对照中，对内核给出的每条路径用 `verify::replay`（独立实现）逐步重放，
  断言全输入弧满足、容量前提、终点精确一致；HTTP 层再与内核 `fire` 交叉核对。
- **令牌加权不变量**：对独立确认为守恒律（残差全 0）的每个权向量，断言路径上每个标识的
  加权和恒定。
- **容量边界**：参考可达集的每个标识都满足 `0≤tokens≤capacity`。
- **真值独立性**：`tests/common` 用与生产代码不同的写法（`BTreeSet` 递归穷举 + 独立 BFS）
  生成可达真值，被测内核不参与生成参考答案；对夹具的**全部容量合法标识**逐一对照判定。
- 测试断言**具体结果与失败类别**（具体序列、具体权向量、具体 HTTP 码与 `error` 码），
  而非仅检查接口可调。

测试命令与覆盖：

```bash
cargo test --lib                 # 30 个单元测试（语义/不变量/BFS/输入/验证/配置）
cargo test --test fixtures_models          # 三个夹具网具体行为
cargo test --test exhaustive_cross_check    # 全标识穷举对照
cargo test --test api                       # 13 个 HTTP 端到端
```

实际执行结果记录见 `docs/VERIFICATION.md`。

---

## 7. P 不变量方法与局限

精确有理数 Gauss-Jordan 求关联矩阵右零空间，化为**本原整数基**；再在有界整数系数
（默认 `|c|_1 ≤ 4`，可用 `invariant_coefficient_bound` 调整）内枚举非负、非零、
归一化去重的候选。`report.method` 与 `candidates_truncated` 显式标注：短候选清单
**不**证明边界之外不存在更大系数的不变量。快速否决只使用*单个已验证残差为 0*
的守恒见证，因此该否决本身是严格的。
