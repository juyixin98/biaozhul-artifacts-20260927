# Interval Abstract Interpreter (Rust / Axum / Serde)

可审查的区间抽象解释器：对一门小型整数语言做静态区间分析，支持分支收窄、
循环（加宽求不动点 + 可选收窄）以及数组下标检查。所有整数量遵循 **有界 i64
检查语义**，算术溢出被当作运行时故障精确分类，而不是与无界数学整数混为一谈。

## 模块划分（每个模块承担实际工作，没有硬编码演示路径）

| 模块 | 职责 |
| --- | --- |
| `src/lang/` | 输入语言：AST、词法分析、递归下降解析；所有语法节点带 1 基行列号与字节偏移 `Span` |
| `src/kernel/interval.rs` | 区间域（含 `±inf`）、格运算、加宽/收窄、比较收窄、i64 边界分类 |
| `src/kernel/state.rs` | 抽象状态：标量→区间、数组→（长度，弱更新元素区间）、显式 bottom |
| `src/kernel/solver.rs` | 迁移函数、条件抽象与收窄、循环加宽/收窄不动点、语义校验、检查记录 |
| `src/concrete.rs` | **独立**的有界 i64 具体解释器（checked 算术），仅供测试作参照，不共享迁移代码 |
| `src/evidence.rs` | 证据验证：不重跑求解器，独立验证不变式是后不动点、结论由证据推出 |
| `src/report.rs` | 可序列化契约：检查结论、结构化证据、不动点轨迹、汇总 |
| `src/api.rs` | Axum 后端：`/v1/analyze`、`/v1/verify`、`/health`、`/v1/version` |
| `tests/` | 独立测试：单元、穷举对照、手写夹具、证据篡改拒绝、HTTP |
| `config/default.toml` | 加宽延迟、收窄次数、迭代上限、具体解释燃料 |

## 四种结论（过近似风险不叫必然错误）

- `safe`：性质在所有可达执行上成立；
- `maybe_violated`：区间**部分**越过边界——可能失败的过近似告警，**不**是已证实的错误；
- `violated`：整个抽象值都在边界外，到达该点的每条执行必然失败；
- `unreachable`：抽象状态为 bottom，没有执行到达该语句。

结论是 `(检查种类, 结构化证据)` 的纯函数（`verdict_of`），证据验证器会重新推导，
报告无法随意给自己贴标签。

## 构建与运行

需要 Rust（在 1.98 上开发）。固定版本见 `Cargo.lock`。

```bash
cargo build --release

# CLI：分析，JSON 报告到 stdout
./target/release/interval-analyzer analyze fixtures/programs/loop_grow.isl \
    --config config/default.toml

# CLI：独立验证一份报告
./target/release/interval-analyzer verify fixtures/programs/loop_grow.isl report.json

# HTTP 服务
./target/release/interval-analyzer serve --config config/default.toml 127.0.0.1:8080
```

### HTTP

```bash
curl -s 127.0.0.1:8080/health
curl -s -X POST 127.0.0.1:8080/v1/analyze \
  -H 'content-type: application/json' \
  --data @fixtures/api/analyze_request.json
```

每个响应都带 `request_id`（`req-<uuid>`）；日志使用同一 `request_id` 字段，
并把**确定失败**（`definite_violation`）与**不确定结论**（`possible_violation`）
分成不同日志行，各自带源码位置与原因。解析/语义错误返回 400 且带行列号。

## 语言（.isl）

```
let x: [-10, 10];          # 非确定输入，闭区间（必须在 i64 内）
array a[10];               # 定长数组，初值全 0
y := x + 1;                # 赋值（:=；不是 =）
z := a[y] * 2;             # 下标读
a[0] := y;                 # 下标写
if x >= 0 { ... } else { ... }
while i < n { ... }
assert x <= 2;
```

支持 `+ - *`、一元负号、比较 `< <= == != >= >`、`&& || !`、`#` 行注释。
`else if` 可用。

## 一键验证脚本

```bash
scripts/verify.sh                     # 构建 + 全部测试 + 夹具证据复验 + 篡改负例
INTERVAL_ANALYZER_E2E=1 scripts/verify.sh   # 另起服务，跑 HTTP 端到端
```

脚本会显式打印被跳过的检查（例如未启动服务时的 HTTP 实网端到端），
不会把未执行的检查写成已通过；HTTP 行为本身由 `tests/api_smoke.rs` 覆盖。

## 测试（参考答案不由被测核心自己生成）

- `tests/interval_unit.rs`：区间格、加宽/收窄、比较收窄、i64 边界的手算断言；
- `tests/concrete_unit.rs`：具体结果与**失败类别**的手写断言（溢出 / 越界含 index 与 len / 断言 / 燃料耗尽）；
- `tests/exhaustive.rs`：对 9 个程序**穷举整个小输入域**（笛卡尔积），独立执行器逐条运行，
  断言每个具体结果被抽象结果包含、每个具体故障被对应位置的 `maybe/violated` 检查覆盖、
  抽象 `safe` 从不在具体执行中失败；“确定”案例额外断言每条具体执行都失败；
- `tests/fixtures_check.rs`：分析结果对照 `fixtures/expected/*.json` 中**手写**的答案；
- `tests/evidence.rs`：真报告通过；篡改结论、缩小循环不变式、篡改源码哈希均被拒绝；
- `tests/api_smoke.rs`：状态码、请求身份、版本、位置、结论分类、配置覆盖。

边界语义细节见 [`docs/SEMANTICS.md`](docs/SEMANTICS.md)，
设计取舍见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。
