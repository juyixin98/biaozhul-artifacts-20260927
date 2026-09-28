# mus-core — 可替换 SAT 求解器的子集极小不可满足核提取服务

纯后端服务。给定一组**带唯一身份**的命题约束（CNF 子句），找出
**子集极小（subset-minimal / irreducible）**的不可满足核（MUS）：

- 核整体 **UNSAT**；
- 移除核中**任意一个**成员后，剩余公式 **SAT**（并附带可独立复核的 SAT 见证）。

技术栈：Rust + Axum 0.7 + Tokio + Serde。求解内核是一个 trait，内置一个
零依赖 DPLL，也可以挂任何讲 DIMACS 协议的本地 CLI 求解器；证据验证默认使用
一个**独立实现**的真值表枚举后端，与提取内核不共享求解代码。

> 术语严格区分：本服务保证的是**子集极小**（移除任一成员即可满足），
> **不是基数最小**（所有核中子句数最少）。`find_all` 是核打包（core packing）
> 多样性策略，**不是** MUS 完全枚举。

---

## 目录结构（每个模块有真实职责）

```
src/
  language.rs          输入语言：约束身份、Literal/Constraint/Cnf/Model、
                       本地 id 格式与 DIMACS 解析、唯一性与变量域校验
  solver/
    mod.rs             Solver trait、SStatus(Sat/Unsat/Unknown 三态)、
                       预算 Budget、协作取消 CancelToken、求解器装配
    builtin.rs         内置 DPLL（单元传播，SAT 见证自检）
    external.rs        外部 DIMACS CLI 求解器适配器（含见证防御性核验）
    oracle.rs          独立真值表枚举后端（用于边界复核，小规模）
  extract/mod.rs       deletion-based 提取；核打包；逐成员极小性见证；
                       每次删减的可审计 trace；预算/取消/UNKNOWN 语义
  verify.rs            独立证据验证：核 UNSAT、逐成员 SAT 且重验见证、
                       全量 trace 复核（抓出撒谎/有 bug 的内核）
  api/mod.rs           HTTP DTO、错误码、AppState、作业与取消注册
  api/handlers.rs      路由与处理器（同步 / 异步作业）
  config.rs            默认值 < JSON 文件 < MUS_* 环境变量
  diagnostics.rs       请求标识 X-Request-Id、敏感公式脱敏
  main.rs / lib.rs     服务入口 / 库装配
tests/
  common/mod.rs        独立答案枚举器、脚本化/撒谎/挂起求解器（测试夹具）
  extract_test.rs      提取算法：重叠核、冗余、预算、取消、UNKNOWN、极小≠最小
  verify_test.rs       独立复核：伪造核、非极小、坏见证、撒谎内核轨迹审计
  solver_test.rs       DPLL 对拍、预算/取消三态、外部 CLI 适配器（本地脚本）
  api_test.rs          真实路由器端到端：具体错误码、请求标识、作业取消
examples/
  library_demo.rs            不经过 HTTP 的库调用示例
  request_extract.json       示例请求体
config/default.json          默认配置
```

## 构建与运行（本地、离线）

依赖已锁定（`Cargo.lock` 随仓库提供）。本机可用 `--offline` 直接构建：

```bash
cargo build --offline --release
cargo run --offline --bin mus-server
# 默认监听 127.0.0.1:8080
```

配置优先级：编译默认值 → `MUS_CONFIG` 指定的 JSON（默认 `config/default.json`）
→ `MUS_*` 环境变量。关键项：

| 配置 / 环境变量                | 含义                                            |
| ------------------------------ | ----------------------------------------------- |
| `bind` / `MUS_BIND`            | 监听地址                                        |
| `solver_kind` / `MUS_SOLVER`   | `builtin`（默认）或 `external`                  |
| `external_binary` / `MUS_EXTERNAL_BINARY` | 外部求解器可执行文件路径             |
| `external_args`                | 传给外部求解器、位于 CNF 文件路径之前的参数    |
| `max_budget` / `MUS_MAX_BUDGET`| 单个请求允许的求解器判定次数上限（默认 100000） |
| `default_budget`               | 请求未给 budget 时使用的值（0 = 不限）          |
| `log_formulas` / `MUS_LOG_FORMULAS` | 是否在日志中打印子句内容；默认**脱敏**，只打 id 与计数 |
| `independent_verification` / `MUS_INDEPENDENT_VERIFICATION` | API 边界是否强制独立复核（默认开） |

挂外部求解器（任何 minisat 兼容、读 CNF 文件、打印
`SATISFIABLE/UNSATISFIABLE` 与 `v ... 0` 的本地二进制）：

```bash
MUS_SOLVER=external MUS_EXTERNAL_BINARY=minisat cargo run --bin mus-server
```

外部后端无法启动、退出异常或输出无法解析时一律记为 **UNKNOWN**，绝不报 UNSAT。

## 运行测试

```bash
cargo test --offline                 # 全部 38 个测试
cargo clippy --offline --all-targets
cargo run --offline --example library_demo
```

## HTTP 接口

| 方法   | 路径                      | 说明                                         |
| ------ | ------------------------- | -------------------------------------------- |
| GET    | `/health`                 | 健康检查                                     |
| GET    | `/v1/solvers`             | 列出主求解器与独立复核后端                   |
| POST   | `/v1/extract`             | 同步提取                                     |
| POST   | `/v1/jobs`                | 创建异步提取作业（202 + job_id）             |
| GET    | `/v1/jobs/:id`            | 查询作业状态与结果                           |
| POST   | `/v1/jobs/:id/cancel`     | 协作取消；保留已验证候选与已取得的证明       |

所有错误返回 `4xx/5xx` 与稳定错误码，并带 `request_id`；可通过请求头
`X-Request-Id` 传入自己的关联标识，响应与日志原样回带。

### 示例请求

结构化约束（推荐，每个约束必须有唯一 `id`）：

```bash
curl -s 127.0.0.1:8080/v1/extract \
  -H 'content-type: application/json' \
  -H 'X-Request-Id: demo-001' \
  -d @examples/request_extract.json
```

或直接贴文本（本地 id 格式；也支持标准 DIMACS，此时自动赋稳定 id `c1,c2,…`）：

```bash
curl -s 127.0.0.1:8080/v1/extract -H 'content-type: application/json' -d '{
  "text": "2\nu: 1 0\nv: -1 0\nr: 2 -2 0\n"
}'
```

本地 id 文本格式：首行可写变量个数；每行 `id: 字面量... 0`，字面量为
DIMACS 带符号整数（`-3` 表示 ¬x₃），`#` 之后为注释。

### 响应要点

```jsonc
{
  "request_id": "demo-001",
  "termination": "completed",        // completed | satisfiable | budget_exhausted
                                     // | cancelled | solver_unknown
  "cores": [
    { "member_ids": ["u", "v"], "size": 2, "verdict": "certified_mus" }
    // verdict: certified_mus | uncertified_unsat_candidate
  ],
  "retained_candidate": [],          // 预算/取消时保留的“已验证 UNSAT 候选”
  "untested": [],                    // 尚未判定到的成员
  "trace": [                         // 每次求解器判定的可审计记录
    { "seq": 1, "round": 1, "phase": "full_check", "tested_id": null,
      "trial_ids": ["p","q","r1","u","v"], "verdict": "unsat",
      "kept": null, "solver": "builtin-dpll", "budget_used_after": 1 }
    // phase: full_check | deletion | minimality_pass
  ],
  "verification": {                  // 独立后端复核结果（默认开启）
    "all_certified": true,
    "cores": [ { "result": "certified_mus", "independent_solver": "independent-bruteforce" } ],
    "trace_audit": [ { "seq": 1, "ok": true, "expected": "unsat", "independent": "unsat" } ]
  }
}
```

异步作业 + 取消（取消不是失败，作业 `succeeded`，结果 `termination=cancelled`）：

```bash
JOB=$(curl -s -X POST 127.0.0.1:8080/v1/jobs -H 'content-type: application/json' \
  -d @examples/request_extract.json | sed -E 's/.*"job_id":"([^"]+)".*/\1/')
curl -s -X POST "127.0.0.1:8080/v1/jobs/$JOB/cancel"
curl -s "127.0.0.1:8080/v1/jobs/$JOB"
```

## 核心语义（关键取舍）

1. **UNKNOWN 不是 UNSAT。** 预算耗尽、取消、外部后端无法判定都是第三态
   `Unknown`，对应终止类别 `budget_exhausted` / `cancelled` / `solver_unknown`，
   绝不会据此产出或删减核。
2. **子集极小 ≠ 基数最小。** 输出只保证移除任一成员即可满足，不保证尺寸最小。
3. **极小性必须有证据。** 核内每个成员 m 都重新求解 `核 \\ {m}` 并保存 SAT
   见证；任一见证拿不到，核降级为 `uncertified_unsat_candidate`。
4. **边界强制独立复核。** `verify` 默认开启：用另一个后端重判核整体 UNSAT、
   逐成员 SAT、**重新求值见证**（不信任不透明模型），并按 trace 里记录的
   id-set 逐条重放判定。测试里有一个"恒报 UNSAT"的撒谎内核，其报告会被
   trace 审计明确标红。
5. **取消保留状态。** 取消到达时，已验证的 UNSAT 候选（`retained_candidate`）
   与已经取得的逐成员见证都保留；尚未判定的成员列入 `untested`。
6. **预算按求解器判定次数计**（不是墙钟），跨整个请求共享，可复现。
7. **find_all 是核打包。** 找到一个核后整体移除再找下一个，因此返回的核两两
   不相交；与已打包核重叠的 MUS 不会被重新发现。要多个核但接受重叠时，
   答案的正确性仍以每个核独立 `certified_mus` 为准。
8. **诊断可关联且脱敏。** 日志与错误带 `request_id`、终止状态、预算用量；默认
   不打印子句内容，只打 id 与计数（`log_formulas=true` 显式放开，长度截断）。
9. **证据形式的范围。** UNSAT 的"独立"来自第二个 SAT 后端对拍，而非
   DRAT/FRAT 反驳证明的机器校验。生产级别的证明检查是 `Solver`/验证接口后
   自然的下一步插件。
10. **规模范围。** 内置 DPLL 为教学/中小规模实例（搜索步数有上限，超限返回
    UNKNOWN）；独立真值表后端仅用于 ≤22 变量的复核/测试。大规模请配置
    `external` 高性能求解器；此时独立复核自动回落到内置 DPLL（与外部实现不同）。

## 输入校验与错误码

`invalid_json`、`missing_formula`、`conflicting_input`、`empty_formula`、
`duplicate_constraint_id`、`duplicate_literal`、`variable_out_of_range`、
`bad_dimacs_header`、`bad_clause_line`、`bad_integer`、`unknown_mode`、
`budget_too_large`、`job_not_found`(404)、`job_not_cancellable`(409)、
`job_capacity_exceeded`(503)。

## 已知未执行 / 范围外项

- 未实现 DRAT/FRAT 证明校验（见上，取舍 9）。
- 内置 DPLL 无子句学习/重启，非工业级性能；外部求解器超时墙钟控制（目前
  外部调用为阻塞执行，判定预算覆盖调用次数，不含子进程墙钟）。
- 作业状态保存在内存中，进程重启即失；无持久化与多实例共享。
- 无鉴权/TLS：定位为本地或受控网络内的纯后端服务。
