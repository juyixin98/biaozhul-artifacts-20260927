# 架构与数据流

```
source (.isl 文本)
   │  src/lang/{lexer,parser}.rs     带 Span 的 AST
   ▼
Vec<Stmt>
   │  src/kernel/solver.rs::validate 声明/名字/形状检查（失败 → 400 semantic_error）
   ▼
┌──────────────────────── SOLVE 趟（不产出检查） ────────────────────────┐
│ Analyzer(Mode::Solve) 逐语句迁移；遇到 while：                            │
│   迭代 X_{k+1} = Widen∘F，直到 X_{k+1} ⊆ X_k；再做 Narrow 收窄            │
│   记录 loop_invariant(span, pre_state, invariant) 与 TraceEvent          │
└──────────────────────────────────────────────────────────────────────┘
   │
   ▼
┌──────────────────────── REPORT 趟（用已稳定不变式） ─────────────────────┐
│ Analyzer(Mode::Report)：同一份迁移代码，但在循环处直接取 SOLVE 的不变式，   │
│ 遍历一次循环体以登记其内部检查；产出 CheckRecord/Observation/exit_state    │
└──────────────────────────────────────────────────────────────────────┘
   │
   ▼
AnalysisReport (serde JSON；program_hash = FNV-1a64(source))
   │
   ├──▶ HTTP /v1/analyze 响应（request_id 关联日志：确定失败 vs 不确定结论分行）
   └──▶ evidence::verify（独立模块）
          1) 源哈希绑定
          2) 结构良构（lo <= hi）
          3) 每个循环不变式的归纳性：pre ⊔ body(inv ∩ cond) ⊆ inv
          4) 用报告自带不变式重跑 REPORT 趟，逐检查比对 (kind, span, verdict)
          5) verdict_of(kind, evidence) 必须等于报告结论
          6) observations / exit_state 一致；summary 计数自洽；轨迹 span 合法
```

## 为什么分两趟

循环体内部的检查（数组下标、溢出、断言）必须在**加宽后又收窄过**的头部不变式
下评估，否则会丢失精度，或被重复登记。先全程序求解、再全程序报告，让检查只在
最终不动点上登记一次；两趟共用迁移代码，避免两套语义漂移。

## 为什么证据验证不重跑加宽

验证器的目标是“报告是否自洽且被其证据支撑”，而不是“再算一遍”。它：

- 只做一轮静默迁移来检验后不动点（归纳性），复杂度与循环无关；
- 用报告中声明的不变式跑 REPORT 趟，重新推导每个检查；
- 从结构化证据纯函数地重算结论。

因此一份被手工缩窄过、或结论与证据不符的报告会被拒绝，即使它“看起来合理”。
真正的可靠性则由独立测试承担：`tests/exhaustive.rs` 用另一份实现（`concrete.rs`）
穷举输入域，验证包含关系与故障类别。

## 状态与区间

- `AbsState { vars: name→Interval, arrays: name→{len, elem}, is_bottom }`。
- `Bottom` 是显式不可达：join 恒等元；给变量赋 bottom 会把整个状态置底
  （确定溢出之后的语句因此不可达）。
- 数组弱更新：`elem' = elem ⊔ value`，长度不变。

## 可追溯性

- 每个 AST 节点、每条检查、每个轨迹事件都带 `Span{line,col,offset,len}`。
- 检查 `id` 在汇总的 `*_violation_ids` / `unreachable_ids` 中可索引回记录。
- 每次分析绑定 `program_hash` 与 `analyzer_version`；响应、日志共享 `request_id`。

## 配置

`config/default.toml` 的字段与 HTTP 请求中的 `config` 覆盖一一对应
（见 `src/config.rs`）：`widen_delay`、`narrow_iters`、`enable_narrowing`、
`max_iterations`、`concrete_fuel`。加载与覆盖均有合法性校验
（如 `widen_delay <= max_iterations`）。
