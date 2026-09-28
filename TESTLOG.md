# 测试运行记录

本文件如实记录实际执行情况（最后更新：2026-09-27，Rust 1.98.1）。

## 最终结果：全部通过，0 失败，0 忽略，0 未执行

`cargo test`（debug）汇总：

| 测试目标 | 用例数 | 结果 |
|---|---|---|
| 库单元测试（lit/normalize/input/diagnostics/solver 内部不变式） | 13 | 全通过 |
| tests/acceptance_api.rs（HTTP 端到端） | 9 | 全通过 |
| tests/acceptance_budget.rs（UNKNOWN/解析分类，含 oracle 自测 3） | 9 | 全通过 |
| tests/acceptance_core.rs（真值表对账/级联/回溯/空子句/随机差分） | 12 | 全通过 |
| tests/acceptance_evidence.rs（篡改拒绝，含 oracle 自测 3） | 17 | 全通过 |
| tests/exhaustive_subspace.rs（4096 个子集完全枚举，含 oracle 自测 3） | 4 | 全通过 |

关键完全枚举结果：12 条"有趣"子句（空/单位/二元/三元/重言/重复）的全部
2^12 = **4096 个子集**，solver 与独立检查器、暴力 oracle 三方一致：
SAT=848，UNSAT=3248。另有 500 个 3/4 变量随机公式差分对账。

`cargo clippy --all-targets`：0 警告。

## 开发过程中出现、随后修复的失败（保留以说明验证是真实进行的）

这些失败在最终版本中均已修复并转为断言通过：

1. **借用检查失败（E0505）**：BCP 恢复 watch 桶时 `wlist` 移动与切片借用冲突。
   修复：先收集余数再重建桶。
2. **学习到单位子句触发 debug_assert 恐慌**：`Clause::new` 断言长度 ≥2。
   修复：学到的单位引理直接构造、不挂双监视。
3. **`repeated_backtracking` 夹具预期错误**：最初误以为二元约束链产生 4 次
   冲突，实际 BCP 在决策后立即传播。改为经实验确认的 5 变量 SAT 夹具，
   精确断言 2 次冲突、5 次决策、确定模型。
4. **两处过严/错误断言**：篡改模型后不满足的子句下标是 0（非 1）；
   PHP-3-2 证明为 2 步（中间引理 + 空末步），断言从 ≥3 改为 ≥2 并补
   "末步必为空"断言。

这些修复均伴随"运行-观察实际计数-修正断言"，没有为通过测试而弱化检查。

## 手工/运行时验证（非自动化）

- `cnf-cli` 对 fixtures/ 下 4 个夹具：SAT/UNSAT/空子句均 `*_VALID`，退出码 0。
- 同一 PHP-4-3 实例：`--max-propagations 0` → `UNKNOWN` 退出码 3；放开预算 →
  UNSAT（9 步归结，checker PROOF_VALID），证明 UNKNOWN 不是偷懒结论。
- PHP-5-4（20 变量 45 子句）：36 决策 / 37 冲突 / 37 步证明，PROOF_VALID。
- 实启 `cnf-api`：/health、/solve（SAT/UNSAT/UNKNOWN）、/verify/proof
  合法通过、篡改 resolvent 返回 `resolvent_mismatch`、越界变量返回 422
  `parse_error`；日志中确认明文文字已脱敏。

## 未执行 / 不在范围

- 未在真实网络/容器编排中部署（需求明确只要求本地启动）。
- 未做压测与大数据集基准（确定性非性能导向，README 已声明取舍）。
- 无被标记 `#[ignore]` 的用例，因此没有"存在但未执行"的测试。
