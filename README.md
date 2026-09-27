# symex-service

一个小型定长整数语言的**符号执行服务**：输入语言只含定长无符号整数、条件与有限循环，
求解内核使用成熟的 SMT 求解器 **Z3**（位向量理论），后端接口为 HTTP（Axum）。
所有输入数据均为本地合成夹具，无生产账号、无真实业务数据。

## 架构与代码组织

按"输入语言 / 求解内核 / 证据验证 / 后端接口"四层组织，另有独立的配置层与测试层：

```
src/
├── lang/            输入语言层
│   ├── lex.rs       手写词法器
│   ├── parse.rs     递归下降语法分析（生成带稳定节点 id 的 AST）
│   ├── check.rs     语义校验 + 位宽推断（无隐式拓宽）
│   ├── ast.rs       AST、确定性的节点编号（同一程序文本 id 稳定）
│   └── types.rs     定长类型 u8/u16/u32/u64 与掩码语义
├── kernel/          求解内核层
│   ├── state.rs     每路径符号状态：SSA 版本化存储 + 双表示路径条件
│   ├── translator.rs AST → Z3 位向量项 + 原生（solver 无关）约束树，收集除零保护点
│   ├── engine.rs    路径探索：调度制 DFS、分支可行性判定、预算与循环展开上限
│   └── report.rs    结果模型：verdict / 路径状态 / 反例 / 预算回显
├── evidence/        证据验证层（完全不依赖 Z3）
│   ├── concrete.rs  独立具体解释器（重放反例、提供测试预言机）
│   ├── native.rs    原生路径条件求值器（小域穷举的独立第二实现）
│   ├── replay.rs    反例重放核验（同一节点、同一失败类别才算复现）
│   └── sem.rs       共享的回绕整数语义原语
├── api/             后端接口层
│   ├── dto.rs       请求/响应类型
│   └── routes.rs    Axum 路由与运行编排（run_id 关联日志）
├── config.rs        配置层：TOML 文件 + SYMEX_* 环境变量覆盖
└── main.rs          symexd 服务入口
tests/               独立测试层（集成测试，预言机来自 evidence 而非 kernel）
examples/programs/   本地合成示例程序
scripts/demo.sh      端到端 curl 演示
config/default.toml  默认配置
```

## 输入语言

```text
param x: u8;                 // 输入参数，类型 u8/u16/u32/u64
let y: u8 = x + 1u8;         // 局部变量（词法作用域，可遮蔽）
y = y * 3u8;                 // 赋值
if (y > 10u8) { ... } else { ... }
while (y < 200u8) { y = y + 1u8; }
assert(y != 0u8, "message"); // 断言（消息可选）
assume(x < 100u8);           // 假设：剪除不可行路径
```

- 运算：`+ - * / % & | ^ << >>`（整数，**回绕语义** mod 2^N）、
  一元 `- ~ !`、比较 `== != < <= > >=`、逻辑 `&& ||`、布尔字面量 `true/false`。
- 字面量：`123`、`0xff`，可带位宽后缀 `255u8`；无后缀字面量从上下文继承位宽，
  无法推断时**报错而非猜测**。
- 整数语义（与 SMT-LIB 位向量一致）：
  - 加减乘按 2^N 回绕；`-x`、`~x` 按位宽定义。
  - 移位量 ≥ 位宽时结果为 0（同 `bvshl`/`bvlshr`）。
  - `/`、`%` 的**除数为 0 是一类失败**（`division_by_zero`），归因到除数表达式节点。
- 每条语句/表达式有稳定节点 id（DFS 先序编号），符号引擎与具体解释器使用同一套 id，
  反例重放据此核验"同一失败位置"。

## 分析语义

- 每条可行路径维护独立符号状态：SSA 版本化变量存储 + 路径条件（Z3 布尔式与原生约束树双表示）。
- 分支事件（if/while 条件、除零保护、断言）逐一做可行性判定；不可行分支被剪除并**记录为
  infeasible**，不会静默丢弃。
- 探索预算：`max_paths`（路径数）、`loop_unroll`（每循环展开上限）、`solver_timeout_ms`
  （单次求解超时）。预算与上限**写入每次分析结果**（`engine`、`budget` 字段）。
- 判定（verdict）：
  - `unsafe`：至少一个反例通过独立具体解释器重放复现（同节点、同类别）。
  - `safe`：所有可达路径探索完毕且断言全部成立。
  - `unknown`：存在未探索完的路径（预算截断、循环展开上限、求解器 unknown）。
    **未覆盖路径永远返回 unknown，绝不返回 safe。**
- 求解器报告失败但重放不能复现时，返回 500 `internal_inconsistency`——
  异常/未知状态不会被统一包装成成功。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/health` | 服务与 Z3 版本 |
| POST | `/v1/analyze` | 符号执行分析（`source` 文本或 `json` 结构体，可带 `engine` 覆盖预算） |
| POST | `/v1/replay` | 用独立具体解释器在给定输入上重放程序 |

示例：

```bash
curl -s localhost:8080/v1/analyze -H 'content-type: application/json' -d '{
  "source": "param x: u8; let y: u8 = x + 1u8; assert(y != 0u8);"
}'
```

响应（节选）：

```json
{
  "run_id": "run-1a0e47…-0",
  "verdict": "unsafe",
  "smt_backend": "z3", "smt_version": "4.8.12.0",
  "engine": {"max_paths": 256, "loop_unroll": 16, "solver_timeout_ms": 2000},
  "budget": {"paths_explored": 2, "max_paths": 256, "truncated": false, "max_depth": 1},
  "path_counts": {"safe": 1, "failed": 1, "infeasible": 0, "unknown": 0,
                  "incomplete_unroll": 0, "incomplete_budget": 0},
  "findings": [{
    "kind": "assertion_failed", "node_id": 4, "line": 1,
    "counterexample": {"x": 255},
    "replay": {"status": "reproduced", "steps": [ … ]},
    "native_path_condition": [ …solver 无关的路径条件… ]
  }]
}
```

每个反例附带：

- `counterexample`：从 SMT 模型提取的具体输入；
- `replay`：独立具体解释器的重放轨迹与结论（`reproduced` 才算数）；
- `native_path_condition` + `native_ssa`：**不依赖 Z3** 的路径条件表示，
  任何第三方都可用普通整数运算独立枚举出触发该失败的全部输入
  （测试中的 `eval_pc_over_domain` 即演示这一点）。

## 构建与运行

### 本地依赖（明确的本地夹具，无需系统安装）

Z3 以本地解包方式提供（本仓库 `.cargo/config.toml` 已指向这些路径）：

```bash
apt-get download libz3-dev libz3-4 libclang1-18 libllvm18 libclang-common-18-dev
dpkg -x libz3-dev_*.deb            ~/opt/z3
dpkg -x libz3-4_*.deb              ~/opt/z3
dpkg -x libclang1-18_*.deb         ~/opt/clang
dpkg -x libllvm18_*.deb            ~/opt/llvm18
dpkg -x libclang-common-18-dev_*.deb ~/opt/clangcommon
```

若放在其他位置，修改 `.cargo/config.toml` 中的绝对路径即可。
`z3` crate 已精确锁定 `=0.12.1`（绑定与本机 libz3 4.8.12 匹配），
其余依赖版本由 `Cargo.lock` 锁定。

### 构建、测试、运行

```bash
cargo build
cargo test                 # 60+ 单元与集成测试
cargo run --bin symexd -- --config config/default.toml
# 或环境变量覆盖：SYMEX_BIND=0.0.0.0:9000 SYMEX_MAX_PATHS=1000 cargo run --bin symexd
bash scripts/demo.sh       # 端到端 curl 演示（分析 + 反例重放）
```

## 验证方式（对应 tests/）

| 验证维度 | 测试文件 | 方法 |
|---|---|---|
| 断言失败检测 | `tests/executor_assert.rs` | 精确节点 id、行号、边界反例值（如 x=255）、重放复现 |
| 互斥路径 | `tests/executor_paths.rs` | 两个/三个互斥分支全部被探索，路径计数与分支决策逐一断言 |
| 整数回绕 | `tests/executor_wrap.rs` | u8/u16 加减乘回绕边界（255+1、0-1、16×16），与机器语义一致 |
| 不可行分支 | `tests/executor_infeasible.rs` | 矛盾条件被剪除、记录为 infeasible、不产生伪反例 |
| 预算与未知 | `tests/executor_budget.rs` | 循环展开上限与路径预算触发 `unknown`（而非 safe），预算回显 |
| 小域穷举对比 | `tests/exhaustive_equiv.rs` | 独立具体解释器穷举全部输入得到失败集；内核反例**sound**（每个反例都在失败集内）且**complete**（各失败路径条件的并集——由独立原生求值器枚举——恰好覆盖失败集） |
| 反例重放 | `tests/replay_verify.rs` | 复现同一失败位置；篡改输入后不复现；具体解释器分类正确性 |
| HTTP 接口 | `tests/api_http.rs` | unsafe/safe/unknown 全链路、400 错误码稳定、健康检查版本、replay 端点 |
| 语言前端 | `tests/lang_parse.rs` | 未声明变量、位宽混用、字面量越界、歧义字面量等具体报错 |

参考答案来源说明：穷举对比的"标准答案"由 `evidence` 层的**独立具体解释器**产生，
路径条件覆盖性由**独立原生求值器**（`evidence/native.rs`，纯手写递归、无 Z3）计算，
均非被测内核自身。

## 日志与可观测性

- 每次分析分配 `run_id`（如 `run-1a0e4705889-0`），请求日志、引擎日志、响应体均携带，
  可据此关联一次运行的全部记录。
- 日志含版本（服务版本、Z3 版本）、预算、路径统计、判定依据；响应中 `path_counts`、
  `budget.truncation_reason` 给出判定依据。
- `RUST_LOG=symex=debug` 可提高日志级别。

## 已知限制

- **无符号整数**：仅 unsigned 语义；有符号比较/溢出未建模。
- **无数组/指针/函数调用**：语言为纯标量、结构化控制流。
- **循环**：按 `loop_unroll` 有界展开，超出即 `unknown`；不做不变量生成/归纳证明。
- **路径爆炸**：`max_paths` 截断后结论为 `unknown`；无状态合并（state merging）。
- **求解超时**：单次 check 超时按 `unknown` 处理（保守），不重试。
- **位宽**：仅 8/16/32/64；小域穷举交叉验证仅适用于小位宽程序（测试内有域大小上限）。
- **确定性**：同一程序文本的节点 id 与探索顺序确定；模型具体取值依赖 Z3，不保证跨版本一致
  （但反例可复现性由重放保证）。
