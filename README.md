# ROBDD 布尔函数后端

一个用 Rust + Axum + Serde 实现的**有序二叉决策图（ROBDD）**后端，支持：

- **构建**：文本语法或 JSON AST → 固定变量序 ROBDD；
- **apply**：`and / or / xor / implies / iff` 二元运算（Shannon 递归 + 记忆化）；
- **限制变量（restrict / cofactor）**：把变量固定为常量；
- **等价查询**：独立真值表预言机判定 + 内核规范边交叉比对，输出**见证赋值**与节点数；
- **垃圾回收**：以命名根为存活集合的 mark-sweep，保留所有根引用；
- **诊断**：每个请求带请求标识与脱敏关键状态，说明接受 / 拒绝 / 无法判定的原因。

所有输入都是本地合成夹具，不依赖任何生产账号或真实业务数据。

---

## 目录结构

```
src/
  lang/                 输入语言（真实职责，不只是接口）
    mod.rs              Expr AST（serde 外部标签 JSON）、变量收集、独立递归解释器
    parser.rs           文本语法递归下降解析器（带字符位置的错误）
  core/                 求解内核
    edge.rs             补集边 Edge：单终节点 + 补集位 + 管理器归属
    mod.rs              BddManager：固定变量序、唯一表、mk 规约、冗余消除、build/evaluate/根
    apply.rs            二元运算 Shannon 递归 + apply 记忆化
    restrict.rs         变量限制（余因子）
    gc.rs               mark-sweep 垃圾回收
  verify.rs             证据验证：独立真值表预言机、变量身份映射、内核/预言机交叉比对
  backend/              Axum HTTP 层
    config.rs           配置（默认值 → JSON 文件 → 环境变量 → CLI）
    diag.rs             请求标识、决策分类、FNV 脱敏指纹、结构化诊断
    state.rs            管理器注册表、不透明边令牌
    handlers.rs         处理器与错误码映射
    app.rs              路由装配
    request_id.rs       请求标识中间件
  main.rs               启动入口
tests/
  exhaustive.rs         穷举证据：3 变量全部 256 个函数、256²×5 种 apply、独立节点数参考
  http_api.rs           端到端 HTTP 协议测试（具体状态码 / 错误类别 / 见证）
config/                 启动配置
samples/                请求样例（可直接 -d @file 使用）
scripts/demo.sh         对着运行中的服务跑 13 步完整流程
docs/
  lang.md               输入语言语法
  architecture.md       ROBDD 不变量与模块设计
  api.md                HTTP 接口参考
  demo-output.txt       真实服务响应留档
  test-output.txt       cargo test 真实输出留档
```

库 crate（`robdd`）与二进制 crate（`robdd-service`）分离：内核可作为普通 Rust 库被复用。

---

## 快速开始

需要 Rust 1.75+（开发使用 1.98，离线可构建，依赖均为本地缓存的 axum 0.8 / serde / tokio）。

```bash
# 构建
cargo build --release

# 运行测试（单元 + 穷举 + HTTP 集成，共 47 个）
cargo test

# 启动（默认 127.0.0.1:8080）
./target/release/robdd-service
# 或临时换端口：
ROBDD_BIND=127.0.0.1:0 ./target/release/robdd-service
# CLI 覆盖：
./target/release/robdd-service --bind 127.0.0.1:9090 --var-cap 16 --log-level debug
```

另开一个终端跑端到端演示（脚本默认连 39273 端口，用 `BASE` 覆盖）：

```bash
BASE=http://127.0.0.1:8080 ./scripts/demo.sh
```

### 最小手工会话

```bash
curl -s -X POST localhost:8080/v1/managers \
  -H 'content-type: application/json' \
  -d '{"variable_order":["a","b","c"]}'
# -> data.manager_id = 1

curl -s -X POST localhost:8080/v1/managers/1/build \
  -H 'content-type: application/json' \
  -d '{"expr":"(a & b) | (!a & c)","root_name":"mux"}'
# -> data.edge = "m1-e2147483654"

curl -s -X POST localhost:8080/v1/equivalence \
  -H 'content-type: application/json' \
  -d '{"left_expr":"a & (b | c)","right_expr":"(a & b) | (a & c)"}'
# -> verdict = "equivalent"，cross_check 中内核与独立真值表一致
```

---

## 测试与证据

`cargo test` 的真实结果（完整留档见 [`docs/test-output.txt`](docs/test-output.txt)）：

```
running 30 tests ... test result: ok. 30 passed; 0 failed   # src/ 各模块单元测试
running  9 tests ... test result: ok.  9 passed; 0 failed   # tests/exhaustive.rs
running  8 tests ... test result: ok.  8 passed; 0 failed   # tests/http_api.rs
```

`cargo clippy --all-targets` 零告警。

### 证据为什么可信（参考答案不是被测核心自己生成的）

- **独立解释器**：`Expr::eval`（`src/lang/mod.rs`）是与 BDD 完全无关的直接递归求值，
  不经过唯一表、apply、补集边任何代码路径。
- **穷举真值表**：`tests/exhaustive.rs` 枚举 3 变量全部 **256** 个布尔函数，
  逐函数核验 `build`/`evaluate`/`restrict`，并对 **256 × 256 × 5** 种
  `(f, g, op)` apply 组合逐赋值比对独立真值表。
- **独立节点数参考**：测试文件内自行实现的真值表掩码余因子切分 +
  带补集边最小节点计数 `ref_node_count`，对 256 个函数逐一与内核
  `reachable_node_count` 核对。
- **独立测试断言具体结果与失败类别**：具体节点数（常量 0、单变量 1、奇偶校验 3）、
  见证的确切赋值（`a & (b|c)` vs `a | (b&c)` 的首个反例 a=1,b=0,c=0）、
  跨管理器边返回 `foreign_manager`、回收边返回 410 `reclaimed_node` 等。
- **内核/预言机分歧显式暴露**：等价接口同时返回内核规范边结论与真值表结论，
  两者不一致时结论为 `inconclusive`（正常情况下永远不会发生）。

---

## 关键设计不变量

详见 [`docs/architecture.md`](docs/architecture.md)，这里列出与需求的对应：

| 需求 | 实现位置 |
|---|---|
| 固定变量序，唯一表按 `(var, low, high)` 规约 | `core/mod.rs::BddManager::mk` |
| 冗余节点消除（low == high 不建节点） | `core/mod.rs::BddManager::mk` |
| 补集/否定统一（单终节点、补集位、低边规范化） | `core/edge.rs` + `mk` + `apply::branches_at` |
| 跨管理器节点不能混用 | `Edge.manager` 归属位 + 内核入口 `require_owner` + 后端令牌双重校验 |
| 回收保留所有根引用 | `core/gc.rs` mark-sweep，从命名根 DFS 标记 |
| 等价结论绑定同一变量身份映射 | `verify::IdentityMapping`（双射校验，重命名后判定） |
| 诊断带请求标识与关键状态、敏感数据脱敏 | `backend/diag.rs` + 每处理器日志 |
| 无法判定而非乱猜 | 身份映射不完整 → `mapping_rejected`；变量超上限 → 独立判定路径 |

---

## 配置

加载顺序（后者覆盖前者）：内置默认 → `ROBDD_CONFIG` 指向的 JSON
（默认 `config/default.json`，存在才读）→ 环境变量 → CLI。

| 配置 | 环境变量 | CLI | 默认 |
|---|---|---|---|
| 监听地址 | `ROBDD_BIND` | `--bind` | `127.0.0.1:8080` |
| 真值表变量上限 | `ROBDD_VAR_CAP` | `--var-cap` | `20`（上限 63） |
| 日志级别 | `ROBDD_LOG_LEVEL` | `--log-level` | `info` |

## License

MIT
