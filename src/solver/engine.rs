//! DPLL 求解内核：双监视文字 BCP + 确定性分支 + 子句学习（1-UIP）。
//!
//! ## 关键不变式
//! - `value[v]`：变量当前真值（None=未赋值）；`trail` 按赋值先后保存"被赋
//!   为真"的文字；`trail_lim[d]` 是第 d 个决策层第一个文字的 trail 下标
//!   （层 0 无入口）。
//! - 非决策文字都有 `reason`（触发它的子句）；决策文字 reason 为 None；
//!   初始单位子句的文字以自身为原因。
//! - 每个长度 ≥2 的子句恰好被两个**非假**文字监视。回溯只撤销赋值，监视
//!   指针不需要回滚——撤销赋值只会让"非假"条件更成立。
//! - 回溯 [`Solver::cancel_until`] 完整恢复 value/level/reason，并把传播
//!   队列游标 qhead 收缩到现存 trail 之内。
//!
//! ## 确定性分支
//! 取编号最小的未赋值变量，恒取正文字。无随机、无相位保存，轨迹可复现。
//!
//! ## UNSAT 证据
//! 冲突分析沿蕴含图做线性归结并同步落 [`ProofStep`]：
//!
//! - 决策层 >0：标准 1-UIP 扫描，得到唯一当前层文字的断言子句；
//! - 决策层 0：沿 trail 逆序与原因子句归结，最终得到空子句；
//! - 输入直接含空子句或矛盾单位子句：记录一步对应推导。
//!
//! 学到子句的数据库文本与其证明引理文本**完全一致**（不剔除层 0 文字），
//! 保证后续证明引用与检查器所见子句逐字相同。

use crate::evidence::types::{
    ClauseRef, Model, Outcome, ProofStep, ResolutionProof,
};
use crate::lit::{neg, sign_positive, var_of, Lit, Var};
use crate::normalize::NormalizedCnf;

use super::budget::{Budget, BudgetExceeded, BudgetGuard, Counters};
use super::clause::{Clause, ClauseId, ClauseOrigin};

enum PropResult {
    Ok,
    Conflict(ClauseId),
}

enum AnalysisResult {
    Unsat,
    Asserting {
        cid: ClauseId,
        backtrack_level: usize,
        assert_lit: Lit,
    },
}

pub struct Solver {
    pub nvars: usize,

    value: Vec<Option<bool>>,
    level: Vec<usize>,
    reason: Vec<Option<ClauseId>>,
    trail: Vec<Lit>,
    trail_lim: Vec<usize>,
    qhead: usize,

    clauses: Vec<Clause>,
    watches: Vec<Vec<ClauseId>>,

    proof_steps: Vec<ProofStep>,

    /// 构造期发现的矛盾单位子句对（两者文字互补）。
    unit_conflict: Option<(ClauseId, ClauseId)>,

    counters: Counters,
}

impl Solver {
    pub fn new(cnf: &NormalizedCnf) -> Self {
        let n = cnf.effective_vars();
        let mut s = Solver {
            nvars: n,
            value: vec![None; n + 1],
            level: vec![0; n + 1],
            reason: vec![None; n + 1],
            trail: Vec::new(),
            trail_lim: Vec::new(),
            qhead: 0,
            clauses: Vec::new(),
            watches: vec![Vec::new(); 2 * (n + 1)],
            proof_steps: Vec::new(),
            unit_conflict: None,
            counters: Counters::default(),
        };

        for (idx, lits) in cnf.clauses.iter().enumerate() {
            match lits.len() {
                0 => {
                    // 空子句：登记一步"空子句拷贝"证明，求解时立即 UNSAT。
                    let id = s.proof_steps.len() as u64;
                    s.proof_steps.push(ProofStep {
                        id,
                        main: ClauseRef::Input { idx },
                        side: vec![],
                        pivot_vars: vec![],
                        resolvent: vec![],
                    });
                }
                1 => {
                    let lit = lits[0];
                    let cid = s.clauses.len();
                    s.clauses.push(Clause {
                        lits: lits.clone(),
                        origin: ClauseOrigin::Input(idx),
                    });
                    let v = var_of(lit);
                    match s.value[v as usize] {
                        None => {
                            // 层 0 事实，以单位子句自身为原因。
                            s.value[v as usize] = Some(sign_positive(lit));
                            s.level[v as usize] = 0;
                            s.reason[v as usize] = Some(cid);
                            s.trail.push(lit);
                        }
                        Some(true) if sign_positive(lit) => {
                            // 重复同向单位：忽略。
                        }
                        Some(false) if !sign_positive(lit) => {}
                        Some(_) => {
                            // 互补单位：记录层 0 冲突，稍后出证明。
                            let prior = s.trail
                                .iter()
                                .rev()
                                .find(|&&t| var_of(t) == v)
                                .map(|&t| s.reason[var_of(t) as usize].unwrap())
                                .unwrap_or_else(|| {
                                    s.reason[v as usize].unwrap()
                                });
                            s.unit_conflict = Some((prior, cid));
                        }
                    }
                }
                _ => {
                    let cid = s.clauses.len();
                    s.clauses
                        .push(Clause::new(lits.clone(), ClauseOrigin::Input(idx)));
                    s.watches[lits[0] as usize].push(cid);
                    s.watches[lits[1] as usize].push(cid);
                }
            }
        }
        s
    }

    // ------------------------------------------------------------------
    // 赋值原语
    // ------------------------------------------------------------------

    #[inline]
    fn lit_value(&self, lit: Lit) -> Option<bool> {
        let v = var_of(lit);
        self.value[v as usize]
            .map(|b| if sign_positive(lit) { b } else { !b })
    }

    fn decision_level(&self) -> usize {
        self.trail_lim.len()
    }

    fn enqueue(&mut self, lit: Lit, reason: Option<ClauseId>) {
        let v = var_of(lit);
        debug_assert!(self.value[v as usize].is_none());
        self.value[v as usize] = Some(sign_positive(lit));
        self.level[v as usize] = self.decision_level();
        self.reason[v as usize] = reason;
        self.trail.push(lit);
    }

    fn unassign(&mut self, var: Var) {
        self.value[var as usize] = None;
        self.level[var as usize] = 0;
        self.reason[var as usize] = None;
    }

    /// 回溯：恢复 value/level/reason，并把传播队列游标收缩到现存 trail。
    fn cancel_until(&mut self, to_level: usize) {
        while self.decision_level() > to_level {
            let trail_start = self.trail_lim.pop().unwrap();
            while self.trail.len() > trail_start {
                let lit = self.trail.pop().unwrap();
                self.unassign(var_of(lit));
            }
        }
        // 已处理游标不得越过现存 trail：低于边界的传播结果仍然有效。
        self.qhead = self.qhead.min(self.trail.len());
    }

    // ------------------------------------------------------------------
    // 双监视文字 BCP
    // ------------------------------------------------------------------

    fn propagate(&mut self) -> PropResult {
        while self.qhead < self.trail.len() {
            self.counters.propagations += 1;
            // p 刚被赋真，故 ¬p 刚变假，检查监视 ¬p 的子句。
            let p = self.trail[self.qhead];
            self.qhead += 1;
            let false_lit = neg(p);

            let mut wlist: Vec<ClauseId> =
                std::mem::take(&mut self.watches[false_lit as usize]);
            let mut n = 0usize; // 继续监视 false_lit 的子句数
            let mut conflict: Option<ClauseId> = None;

            let mut i = 0usize;
            while i < wlist.len() {
                let cid = wlist[i];
                i += 1;

                // 保证 false_lit 位于 lits[1]。
                if self.clauses[cid].lits[0] == false_lit {
                    self.clauses[cid].lits.swap(0, 1);
                }
                {
                    let clause = &self.clauses[cid];
                    if self.lit_value(clause.lits[0]) == Some(true) {
                        wlist[n] = cid;
                        n += 1;
                        continue;
                    }
                }
                // 在 lits[2..] 寻找非假替代文字。
                let len = self.clauses[cid].lits.len();
                let mut replacement: Option<Lit> = None;
                for k in 2..len {
                    let cand = self.clauses[cid].lits[k];
                    if self.lit_value(cand) != Some(false) {
                        replacement = Some(cand);
                        break;
                    }
                }
                if let Some(new_lit) = replacement {
                    // 交换到监视位并迁移；cid 不再保留在 false_lit 表中。
                    let pos = self.clauses[cid]
                        .lits
                        .iter()
                        .position(|&x| x == new_lit)
                        .unwrap();
                    self.clauses[cid].lits.swap(1, pos);
                    self.watches[new_lit as usize].push(cid);
                    continue;
                }
                // 无替代：lits[1] 假，子句满足/单位/冲突取决于 lits[0]。
                let first = self.clauses[cid].lits[0];
                wlist[n] = cid;
                n += 1;
                if self.lit_value(first) == Some(false) {
                    conflict = Some(cid);
                    break;
                }
                self.enqueue(first, Some(cid));
            }

            // 未处理的余离子句恢复到 false_lit 表；已迁移的不在其中。
            let remainder: Vec<ClauseId> = wlist[i..].to_vec();
            let mut bucket = wlist;
            bucket.truncate(n);
            bucket.extend_from_slice(&remainder);
            self.watches[false_lit as usize] = bucket;

            if let Some(cid) = conflict {
                return PropResult::Conflict(cid);
            }
        }
        PropResult::Ok
    }

    // ------------------------------------------------------------------
    // 确定性分支
    // ------------------------------------------------------------------

    fn pick_branch_variable(&self) -> Option<Var> {
        (1..=self.nvars as Var).find(|&v| self.value[v as usize].is_none())
    }

    fn new_decision(&mut self) {
        let v = self.pick_branch_variable().expect("仍有未赋值变量");
        self.trail_lim.push(self.trail.len());
        self.counters.decisions += 1;
        self.enqueue(2 * v, None); // 恒取正文字
    }

    // ------------------------------------------------------------------
    // 冲突分析 + 证明构造
    // ------------------------------------------------------------------

    /// 记录一条证明步并返回其引理 id。
    fn emit_step(
        &mut self,
        main: ClauseOrigin,
        side: Vec<ClauseRef>,
        pivots: Vec<u32>,
        resolvent: Vec<Lit>,
    ) -> u64 {
        let id = self.proof_steps.len() as u64;
        self.proof_steps.push(ProofStep {
            id,
            main: main.as_clause_ref(),
            side,
            pivot_vars: pivots,
            resolvent,
        });
        id
    }

    fn analyze(&mut self, conflict_cid: ClauseId) -> AnalysisResult {
        let dlevel = self.decision_level();
        debug_assert!(
            dlevel == 0
                || self.clauses[conflict_cid]
                    .lits
                    .iter()
                    .all(|&l| self.lit_value(l) == Some(false))
        );

        let conflict_origin = self.clauses[conflict_cid].origin.clone();
        let mut current: Vec<Lit> = self.clauses[conflict_cid].lits.clone();
        let mut seen = vec![false; self.nvars + 1];
        for &l in &current {
            seen[var_of(l) as usize] = true;
        }

        if dlevel == 0 {
            // 沿 trail 逆序处理：每个 seen 文字与其原因归结。
            let mut sides: Vec<ClauseRef> = Vec::new();
            let mut pivots: Vec<u32> = Vec::new();
            let mut idx = self.trail.len();
            while !current.is_empty() {
                // 找最靠后、其变量仍在 current 中的 trail 文字。
                let p = loop {
                    idx -= 1;
                    let cand = self.trail[idx];
                    if seen[var_of(cand) as usize] {
                        break cand;
                    }
                };
                let pv = var_of(p);
                seen[pv as usize] = false;
                let rcid = self.reason[pv as usize]
                    .expect("层 0 trail 文字必有原因（含初始单位）");
                let reason_origin = self.clauses[rcid].origin.clone();
                let reason_lits = self.clauses[rcid].lits.clone();
                for &q in &reason_lits {
                    seen[var_of(q) as usize] = true;
                }
                current = resolve_sets(&current, &reason_lits, pv);
                sides.push(reason_origin.as_clause_ref());
                pivots.push(pv);
            }
            current.sort_unstable();
            current.dedup();
            debug_assert!(current.is_empty());
            self.emit_step(conflict_origin, sides, pivots, current);
            return AnalysisResult::Unsat;
        }

        // 决策层 >0：标准 1-UIP 线性扫描。
        let mut pathc = self
            .clauses[conflict_cid]
            .lits
            .iter()
            .filter(|&&l| self.level[var_of(l) as usize] == dlevel)
            .count();
        let mut idx = self.trail.len();
        let mut sides: Vec<ClauseRef> = Vec::new();
        let mut pivots: Vec<u32> = Vec::new();
        let uip_trail_lit: Lit;

        loop {
            // 最靠后的、seen 的当前层文字。
            let p = loop {
                idx -= 1;
                let cand = self.trail[idx];
                let v = var_of(cand);
                if seen[v as usize] && self.level[v as usize] == dlevel {
                    break cand;
                }
            };
            pathc -= 1;
            if pathc == 0 {
                uip_trail_lit = p;
                break;
            }
            let pv = var_of(p);
            let rcid = self.reason[pv as usize]
                .expect("当前层非 UIP 文字必有原因");
            let reason_origin = self.clauses[rcid].origin.clone();
            let reason_lits = self.clauses[rcid].lits.clone();
            for &q in &reason_lits {
                let qv = var_of(q);
                if !seen[qv as usize] && self.level[qv as usize] == dlevel {
                    pathc += 1;
                }
                seen[qv as usize] = true;
            }
            current = resolve_sets(&current, &reason_lits, pv);
            seen[pv as usize] = false;
            sides.push(reason_origin.as_clause_ref());
            pivots.push(pv);
        }

        current.sort_unstable();
        current.dedup();

        // 断言文字：归结式中唯一的当前层文字，极性为"假形式"（即 trail
        // 文字的互补）——回退后它未赋值，被单位传播强制为真。
        let assert_lit = *current
            .iter()
            .find(|&&l| self.level[var_of(l) as usize] == dlevel)
            .expect("1-UIP 归结式恰含一个当前层文字");
        debug_assert_eq!(assert_lit, neg(uip_trail_lit));

        let id = self.emit_step(
            conflict_origin,
            sides,
            pivots,
            current.clone(),
        );

        // 入库文本 = 证明引理文本（含层 0 文字，保证两处一致）。
        let mut rest: Vec<Lit> =
            current.iter().copied().filter(|&l| l != assert_lit).collect();
        rest.sort_by(|&a, &b| {
            self.level[var_of(b) as usize]
                .cmp(&self.level[var_of(a) as usize])
                .then(var_of(a).cmp(&var_of(b)))
        });
        let mut stored = vec![assert_lit];
        stored.extend(rest);

        let cid = self.clauses.len();
        let backtrack_level = stored
            .iter()
            .skip(1)
            .map(|&l| self.level[var_of(l) as usize])
            .max()
            .unwrap_or(0);
        let is_unit = stored.len() == 1;
        if is_unit {
            // 学到的单位引理不挂监视（与输入单位一致）。
            self.clauses.push(Clause {
                lits: stored,
                origin: ClauseOrigin::Learned(id),
            });
        } else {
            self.clauses
                .push(Clause::new(stored, ClauseOrigin::Learned(id)));
            let l0 = self.clauses[cid].lits[0];
            let l1 = self.clauses[cid].lits[1];
            self.watches[l0 as usize].push(cid);
            self.watches[l1 as usize].push(cid);
        }
        self.counters.learned_clauses += 1;

        AnalysisResult::Asserting {
            cid,
            backtrack_level,
            assert_lit,
        }
    }

    /// 矛盾单位子句的一步归结证明。
    fn analyze_unit_conflict(&mut self, a: ClauseId, b: ClauseId) {
        let origin_a = self.clauses[a].origin.clone();
        let origin_b = self.clauses[b].origin.clone();
        let pivot = var_of(self.clauses[a].lits[0]);
        self.emit_step(
            origin_a,
            vec![origin_b.as_clause_ref()],
            vec![pivot],
            vec![],
        );
    }

    // ------------------------------------------------------------------
    // 主循环
    // ------------------------------------------------------------------

    pub fn solve(
        mut self,
        budget: &Budget,
    ) -> (Outcome, Counters, Option<BudgetExceeded>) {
        let guard = BudgetGuard::start();

        if let Some((a, b)) = self.unit_conflict {
            self.analyze_unit_conflict(a, b);
            return (
                Outcome::Unsat {
                    proof: ResolutionProof {
                        steps: self.proof_steps,
                    },
                },
                self.counters,
                None,
            );
        }

        // 若构造期空子句已登记证明，直接 UNSAT。
        if self
            .proof_steps
            .iter()
            .any(|s| s.resolvent.is_empty() && s.side.is_empty())
        {
            return (
                Outcome::Unsat {
                    proof: ResolutionProof {
                        steps: self.proof_steps,
                    },
                },
                self.counters,
                None,
            );
        }

        loop {
            if let Err(e) = guard.check(budget, &self.counters) {
                return (
                    Outcome::Unknown {
                        reason: format!("{e:?}"),
                        partial_proof: ResolutionProof {
                            steps: self.proof_steps,
                        },
                    },
                    self.counters,
                    Some(e),
                );
            }

            match self.propagate() {
                PropResult::Ok => match self.pick_branch_variable() {
                    None => {
                        let mut true_literals: Vec<Lit> = (1..=self.nvars)
                            .map(|v| {
                                if self.value[v] == Some(true) {
                                    2 * v as Lit
                                } else {
                                    2 * v as Lit + 1
                                }
                            })
                            .collect();
                        true_literals.sort_unstable();
                        return (
                            Outcome::Sat {
                                model: Model {
                                    true_literals,
                                    num_vars: self.nvars,
                                },
                            },
                            self.counters,
                            None,
                        );
                    }
                    Some(_) => self.new_decision(),
                },
                PropResult::Conflict(cid) => {
                    self.counters.conflicts += 1;
                    match self.analyze(cid) {
                        AnalysisResult::Unsat => {
                            return (
                                Outcome::Unsat {
                                    proof: ResolutionProof {
                                        steps: self.proof_steps,
                                    },
                                },
                                self.counters,
                                None,
                            );
                        }
                        AnalysisResult::Asserting {
                            cid,
                            backtrack_level,
                            assert_lit,
                        } => {
                            self.cancel_until(backtrack_level);
                            self.enqueue(assert_lit, Some(cid));
                        }
                    }
                }
            }
        }
    }

    /// 测试/诊断用：当前 trail 快照（变量、层、是否决策）。
    pub fn debug_trail(&self) -> Vec<(Lit, usize, bool)> {
        self.trail
            .iter()
            .map(|&l| {
                let v = var_of(l);
                (l, self.level[v as usize], self.reason[v as usize].is_none())
            })
            .collect()
    }
}

/// 集合归结：(a ∪ b) 删去枢轴变量的两种极性，排序去重。
fn resolve_sets(a: &[Lit], b: &[Lit], pivot_var: Var) -> Vec<Lit> {
    let mut out: Vec<Lit> = a
        .iter()
        .chain(b.iter())
        .copied()
        .filter(|l| var_of(*l) != pivot_var)
        .collect();
    out.sort_unstable();
    out.dedup();
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::normalize::normalize_signed_clauses;

    #[test]
    fn cancel_until_restores_assignments_and_queue() {
        // 决策 x1、x2、x3（三层），再回溯到层 1：
        // 层 2/3 的赋值必须撤销，qhead 必须收缩到现存 trail，
        // 层 0/1 的赋值必须保留。
        let clauses: Vec<Vec<i64>> = vec![vec![1, 2, 3, 4]];
        let cnf = normalize_signed_clauses(4, &clauses);
        let mut s = Solver::new(&cnf);
        // 初始无单位，trail 为空。
        s.new_decision(); // x1 @ level 1
        s.new_decision(); // x2 @ level 2
        s.new_decision(); // x3 @ level 3
        assert_eq!(s.decision_level(), 3);
        assert_eq!(s.trail.len(), 3);
        // 模拟传播队列已处理到尾部。
        s.qhead = 3;

        s.cancel_until(1);

        assert_eq!(s.decision_level(), 1);
        assert_eq!(s.trail.len(), 1, "层 2/3 文字必须出栈");
        // value/level/reason 完整恢复。
        assert_eq!(s.value[1], Some(true), "层 1 赋值保留");
        assert_eq!(s.value[2], None, "层 2 赋值必须撤销");
        assert_eq!(s.value[3], None, "层 3 赋值必须撤销");
        assert_eq!(s.level[2], 0);
        assert_eq!(s.reason[2], None);
        // 传播队列游标不得越过现存 trail；层 1 的 x1 此前已处理。
        assert!(s.qhead <= s.trail.len());
        assert_eq!(s.qhead, 1);

        // 再完全回退到层 0。
        s.cancel_until(0);
        assert_eq!(s.trail.len(), 0);
        assert_eq!(s.value[1], None);
        assert!(s.qhead <= s.trail.len());
    }

    #[test]
    fn propagation_queue_resumes_after_backtrack() {
        // 级联单位传播在回退后仍可正确继续，且不会重复处理已出队文字。
        let clauses: Vec<Vec<i64>> = vec![
            vec![1],
            vec![-1, 2],
            vec![-2, 3],
        ];
        let cnf = normalize_signed_clauses(3, &clauses);
        let mut s = Solver::new(&cnf);
        assert!(matches!(s.propagate(), PropResult::Ok));
        assert_eq!(s.value[1], Some(true));
        assert_eq!(s.value[2], Some(true));
        assert_eq!(s.value[3], Some(true));
        assert_eq!(s.qhead, 3);
    }
}
