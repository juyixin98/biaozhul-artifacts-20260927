# 架构与判定语义

## 分层

```
HTTP (Axum)                se-server/src/api.rs
  ├─ 配置 configs/server.toml / env / CLI      se-server/src/config.rs
  ├─ /analyze   ── 引擎分析 ── 独立重放裁决（可选穷举 oracle）
  ├─ /verify/replay ── 仅具体解释器
  └─ /oracle    ── 小域穷举真值
        │
        ▼
符号执行引擎                se-engine/src/engine.rs
  工作列表(路径状态) + 续体栈；分支/循环展开；预算与 cuts；事件日志
        │  符号项 se-engine/src/symbolic.rs（AST→Term，收集 div/overflow 守卫）
        ▼
求解内核                    se-solver/
  Term → SMT-LIB 2 (QF_BV)（smt.rs）→ Z3 CLI 子进程（solver.rs）
        ▲
        │ 生产用 Z3Cli；测试可注入 BruteSolver（独立枚举）
具体语义（与符号侧完全独立）  se-lang/src/interp.rs（重放/穷举共用）
证据验证 + 穷举 oracle       se-verify/src/{replay,oracle}.rs
```

关键解耦：引擎只依赖 `SmtSolver` trait；`se-lang` 的具体解释器不引用任何
符号代码，因此“验证证据”与“穷举真值”都不经过被测引擎。

## 路径状态与约束

每条路径持有：符号环境（名字→Term）、路径条件 `pc: Vec<Term>`（含声明输入域
约束）、续体栈、待处理语义守卫（除零/溢出）。分支处理：

1. 求值条件得到布尔项 `t`；
2. 分别查询 `pc ∧ t` 与 `pc ∧ ¬t`；
3. 仅 `sat` 的一侧被进入/派生；双 sat 时一侧入工作列表（else/loop-exit），
   另一侧当前继续；任一查询 `unknown` → 该路径记 `solver_unknown` cut 并
   终止为 unknown。

循环按 while 条件逐次展开，每次进入都查询可进入/可退出并分叉；单路径进入
迭代数超过 `max_loop_unroll` 即 `loop_unroll` cut。

断言与守卫：查询 `pc ∧ bad`（断言的 bad = cond==0；守卫的 bad = 否定其安全
条件）。`sat` 给出反例模型，`unsat` 表示该路径上不可能失败，`unknown` 为 cut。
`ite` 未选中分支中的守卫会被条件蕴含弱化（`c⇒g` / `¬c⇒g`）。

## 判定如何产生

* 引擎报告 `violation`（存在候选证据）/ `holds`（无证据且无 cut、无 unknown
  查询）/ `unknown`（存在任何 cut 或求解器 unknown）。
* `se-verify` 对每个候选反例执行独立具体重放：失败**类别**与**语句 id** 必须
  完全相同、输入必须落在声明域内。全部确认后最终判定才是 `violation`；
  引擎判 violation 但零确认 → 最终 `unknown`。
* 穷举 oracle 截断时也判 `unknown`，绝不把没跑完的域当成安全。

## 日志与可关联性

每次运行生成 `request_id`（可由调用方提供并回显）、`run_id`（时间戳）、
`program_id`（规范 JSON 的 FNV 内容哈希）。`report.steps` 按序记录 start →
check(sat/unsat/unknown 及求解器版本) → fork → terminal/violation/cut →
finish，并回显预算计数；服务端 tracing 日志携带相同 id。

## 失败类别

`assertion`（assert 条件为 0）、`div_by_zero`（udiv/urem/sdiv/srem 除数 0）、
`overflow`（trap 模式加减乘溢出、sdiv/srem 的 INT_MIN/-1、neg INT_MIN）、
`step_limit`（具体解释器步数上限）。
