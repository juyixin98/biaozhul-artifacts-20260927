//! 求解内核：带双监视文字（two-watched-literal）单位传播的 DPLL。
//!
//! 关键不变量（回溯安全性）：
//! - 所有赋值记录在单条 `trail` 上；`trail_lim[d]` 是第 d+1 个决策层的起点。
//! - 传播队列就是 `trail[qhead..]`；回撤销 trail 尾部并把 `qhead` 置为新的 trail 长度，
//!   赋值与传播队列一起恢复，残留的旧传播不会泄漏到新分支。
//! - 子句的两个被监视文字恒位于 `lits[0]`、`lits[1]`。
//! - 冲突分析按 1-UIP 做线性消解；每一步消解都记录进 [`ResolutionProof`]，
//!   UNSAT 时得到空子句。证明只引用输入子句或此前派生的子句，可被独立复查。
//!
//! 分支策略是确定性的：按“变量在规范化子句中的出现次数降序、次数相同按变量号升序”
//! 静态排序，未赋值时选序列中第一个变量，固定先赋假（负极性）。同样的输入与预算
//! 必然产生同样的搜索路径与证明。

use std::time::{Duration, Instant};

use serde::Serialize;

use crate::cnf::{Formula, Lit, Var};
use crate::evidence::{DerivedClause, ResolutionProof};

/// 子句在数据库中的下标。
type ClauseId = usize;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Val {
    Unassigned,
    True,
    False,
}

impl Val {
    fn is_false(self) -> bool {
        self == Val::False
    }
}

/// 搜索/时间预算。任一耗尽即返回 UNKNOWN，而不是 UNSAT。
#[derive(Debug, Clone, Default)]
pub struct SolveLimits {
    pub max_decisions: Option<u64>,
    pub time_limit: Option<Duration>,
}

/// 预算耗尽的具体原因。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StopReason {
    Completed,
    DecisionBudget,
    TimeBudget,
}

#[derive(Debug, Clone, Serialize)]
pub struct SolveDiagnostics {
    pub num_vars: usize,
    pub num_input_clauses: usize,
    pub decisions: u64,
    /// 传播期间被检查的“监视子句”次数（传播工作量的近似度量）。
    pub watched_inspections: u64,
    pub conflicts: u64,
    pub learned_clauses: u64,
    pub final_decision_level: u32,
    pub stop_reason: StopReason,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Sat,
    Unsat,
    Unknown,
}

#[derive(Debug, Clone)]
pub struct SolveOutcome {
    pub status: Status,
    /// SAT 时的完整赋值，下标即变量号（0 号位占位为 false）。
    pub model: Option<Vec<bool>>,
    /// UNSAT 时的线性消解推导记录。
    pub proof: Option<ResolutionProof>,
    pub diagnostics: SolveDiagnostics,
}

/// 子句出处：输入子句（带规范化后的序号）或学习子句（带证明 id）。
#[derive(Debug, Clone)]
enum Origin {
    Input(usize),
    Learned(String),
}

impl Origin {
    fn as_ref(&self) -> String {
        match self {
            Origin::Input(i) => format!("i{i}"),
            Origin::Learned(id) => id.clone(),
        }
    }
}

pub struct Solver<'a> {
    n: usize,
    formula: &'a Formula,
    // 按变量号索引（0 号不用）。
    value: Vec<Val>,
    level: Vec<u32>,
    reason: Vec<Option<ClauseId>>,
    // 真值文字的 trail；trail[qhead..] 即传播队列。
    trail: Vec<Lit>,
    trail_lim: Vec<usize>,
    qhead: usize,
    // 子句数据库（学习子句只追加），第 0、1 个文字为被监视文字。
    db: Vec<Vec<Lit>>,
    origins: Vec<Origin>,
    // watches[lidx(l)]：监视文字 l 的子句；l 变假时处理。
    watches: Vec<Vec<ClauseId>>,
    // 确定性分支顺序。
    order: Vec<Var>,
    // 证明记录。
    derived: Vec<DerivedClause>,
    // 统计。
    decisions: u64,
    watched_inspections: u64,
    conflicts: u64,
    started_at: Instant,
}

/// 监视表下标：正文字占偶数位，负文字占奇数位。
fn lidx(l: Lit) -> usize {
    let v = (l.unsigned_abs() as usize) - 1;
    if l > 0 {
        2 * v
    } else {
        2 * v + 1
    }
}

impl<'a> Solver<'a> {
    pub fn new(formula: &'a Formula) -> Self {
        let n = formula.num_vars;
        let mut score = vec![0u32; n + 1];
        for c in &formula.clauses {
            for &l in &c.lits {
                score[l.unsigned_abs() as usize] += 1;
            }
        }
        let mut order: Vec<Var> = (1..=n).collect();
        // 出现次数降序，次数相同变量号升序——完全确定。
        order.sort_by(|&x, &y| score[y].cmp(&score[x]).then(x.cmp(&y)));

        Solver {
            n,
            formula,
            value: vec![Val::Unassigned; n + 1],
            level: vec![0; n + 1],
            reason: vec![None; n + 1],
            trail: Vec::new(),
            trail_lim: Vec::new(),
            qhead: 0,
            db: Vec::new(),
            origins: Vec::new(),
            watches: vec![Vec::new(); 2 * n],
            order,
            derived: Vec::new(),
            decisions: 0,
            watched_inspections: 0,
            conflicts: 0,
            started_at: Instant::now(),
        }
    }

    pub fn solve(mut self, limits: &SolveLimits) -> SolveOutcome {
        let mk_diag = |s: &Solver<'_>, stop: StopReason| SolveDiagnostics {
            num_vars: s.n,
            num_input_clauses: s.formula.clauses.len(),
            decisions: s.decisions,
            watched_inspections: s.watched_inspections,
            conflicts: s.conflicts,
            learned_clauses: s.derived.len() as u64,
            final_decision_level: s.trail_lim.len() as u32,
            stop_reason: stop,
        };

        // 1) 安装输入子句；记录空子句；单位子句进入第 0 层队列。
        let mut initial_empty: Option<ClauseId> = None;
        let mut pending_level0_conflict: Option<ClauseId> = None;
        for (i, c) in self.formula.clauses.iter().enumerate() {
            let cid = self.db.len();
            self.db.push(c.lits.clone());
            self.origins.push(Origin::Input(i));
            if c.lits.is_empty() {
                initial_empty.get_or_insert(cid);
            } else if c.lits.len() == 1 {
                if self.enqueue(c.lits[0], Some(cid)).is_err() {
                    // 与已入队的第 0 层单位矛盾。
                    pending_level0_conflict = Some(cid);
                }
            } else {
                let (a, b) = (c.lits[0], c.lits[1]);
                self.watches[lidx(a)].push(cid);
                self.watches[lidx(b)].push(cid);
            }
        }

        // 空子句直接定死 UNSAT；空推导即足以让检查器定位该输入空子句。
        if let Some(cid) = initial_empty {
            return SolveOutcome {
                status: Status::Unsat,
                model: None,
                proof: Some(ResolutionProof {
                    derived_clauses: Vec::new(),
                    empty_clause_ref: self.origins[cid].as_ref(),
                }),
                diagnostics: mk_diag(&self, StopReason::Completed),
            };
        }

        // 2) 主循环：传播 → 冲突分析/回退 → 决策。
        loop {
            let conflict = match pending_level0_conflict.take() {
                Some(cid) => Some(cid),
                None => self.propagate(),
            };

            if let Some(cid) = conflict {
                self.conflicts += 1;
                let cur_level = self.trail_lim.len() as u32;

                if cur_level == 0 {
                    // 第 0 层冲突：消解必须推到空子句才算 UNSAT。
                    let id_offset = self.derived.len();
                    let entries = self.analyze_level0(cid, id_offset);
                    let empty_ref = if entries.is_empty() {
                        // 冲突子句本身就是输入空子句（防御性分支；安装期通常已提前返回）。
                        self.origins[cid].as_ref()
                    } else {
                        entries.last().unwrap().id.clone()
                    };
                    self.derived.extend(entries);
                    let diag = mk_diag(&self, StopReason::Completed);
                    return SolveOutcome {
                        status: Status::Unsat,
                        model: None,
                        proof: Some(ResolutionProof {
                            derived_clauses: self.derived,
                            empty_clause_ref: empty_ref,
                        }),
                        diagnostics: diag,
                    };
                }

                let (learned, blevel, start_ref, ops) = self.analyze(cid, cur_level);

                // 回退并加入学习子句，然后在回溯层断言其唯一的当前层文字（1-UIP）。
                self.cancel_until(blevel);
                let new_cid = self.db.len();
                let mut clause = learned;
                if clause.len() >= 2 {
                    // lits[0] 已是 UIP；把回溯层中最高层的文字放到 lits[1]。
                    let mut best = 1usize;
                    for k in 2..clause.len() {
                        if self.var_level(clause[k]) > self.var_level(clause[best]) {
                            best = k;
                        }
                    }
                    clause.swap(1, best);
                    self.watches[lidx(clause[0])].push(new_cid);
                    self.watches[lidx(clause[1])].push(new_cid);
                }
                let uip = clause[0];
                let id = format!("d{}", self.derived.len());
                self.db.push(clause);
                self.origins.push(Origin::Learned(id.clone()));
                self.derived.push(DerivedClause {
                    id,
                    literals: self.sorted_snapshot(&self.db[new_cid]),
                    start_ref,
                    resolvents: ops,
                });
                // 断言 UIP；此时它必须未赋值。
                self.enqueue(uip, Some(new_cid))
                    .expect("UIP unassigned after backtrack");
                continue;
            }

            // 无冲突：所有变量赋值完毕即可给出模型。
            if self.trail.len() == self.n {
                let model = self.build_model();
                return SolveOutcome {
                    status: Status::Sat,
                    model: Some(model),
                    proof: None,
                    diagnostics: mk_diag(&self, StopReason::Completed),
                };
            }

            // 预算检查放在“准备做下一次决策”的位置：
            // 传播得到的结论（含第 0 层 UNSAT）不受决策预算限制；
            // 一旦真的需要第 N+1 次分叉而预算只给 N，立即 UNKNOWN。
            if let Some(m) = limits.max_decisions {
                if self.decisions >= m {
                    return SolveOutcome {
                        status: Status::Unknown,
                        model: None,
                        proof: None,
                        diagnostics: mk_diag(&self, StopReason::DecisionBudget),
                    };
                }
            }
            if let Some(t) = limits.time_limit {
                if self.started_at.elapsed() >= t {
                    return SolveOutcome {
                        status: Status::Unknown,
                        model: None,
                        proof: None,
                        diagnostics: mk_diag(&self, StopReason::TimeBudget),
                    };
                }
            }

            self.decide();
        }
    }

    // ---------- 赋值与传播 ----------

    fn lit_value(&self, l: Lit) -> Val {
        let v = self.value[l.unsigned_abs() as usize];
        if l > 0 {
            v
        } else if v == Val::True {
            Val::False
        } else if v == Val::False {
            Val::True
        } else {
            Val::Unassigned
        }
    }

    fn var_level(&self, l: Lit) -> u32 {
        self.level[l.unsigned_abs() as usize]
    }

    /// 把文字 l 作为真值入队。已赋相反值时返回 Err（调用方据此识别冲突）。
    fn enqueue(&mut self, l: Lit, reason: Option<ClauseId>) -> Result<(), ()> {
        let v = l.unsigned_abs() as usize;
        match self.lit_value(l) {
            Val::True => Ok(()), // 重复断言，幂等
            Val::False => Err(()),
            Val::Unassigned => {
                self.value[v] = if l > 0 { Val::True } else { Val::False };
                self.level[v] = self.trail_lim.len() as u32;
                self.reason[v] = reason;
                self.trail.push(l);
                Ok(())
            }
        }
    }

    /// 双监视文字传播。返回 Some(cid) 表示冲突子句。
    fn propagate(&mut self) -> Option<ClauseId> {
        while self.qhead < self.trail.len() {
            let p = self.trail[self.qhead];
            self.qhead += 1;
            // p 变真 ⇒ 被监视文字 -p 变假，检查对应监视表。
            let false_lit = -p;
            let wi = lidx(false_lit);

            // 摘下整张表逐项处理：保留的项前移，换防的项进别的表，冲突时保留剩余全部。
            let mut wlist = std::mem::take(&mut self.watches[wi]);
            let mut i = 0usize;
            let mut j = 0usize;
            let mut conflict = None;
            while i < wlist.len() {
                let cid = wlist[i];
                self.watched_inspections += 1;

                // 保证 false_lit 在 0 号位。
                if self.db[cid][0] != false_lit {
                    let lits = &mut self.db[cid];
                    lits.swap(0, 1);
                }
                let other = self.db[cid][1];
                if self.lit_value(other) == Val::True {
                    wlist[j] = cid;
                    j += 1;
                    i += 1;
                    continue;
                }

                // 在非监视位找一个“未假”的文字换防。
                let mut replacement = None;
                for k in 2..self.db[cid].len() {
                    if !self.lit_value(self.db[cid][k]).is_false() {
                        replacement = Some(k);
                        break;
                    }
                }

                if let Some(k) = replacement {
                    self.db[cid].swap(0, k);
                    let new_lit = self.db[cid][0];
                    // 解除对 false_lit 的监视，转去监视 new_lit。
                    self.watches[lidx(new_lit)].push(cid);
                    i += 1;
                    continue;
                }

                // 没有替代文字：另一个监视文字要么是单位、要么也假（冲突）。
                wlist[j] = cid;
                j += 1;
                i += 1;
                if self.lit_value(other) == Val::Unassigned {
                    // 单位传播：reason 即本子句。
                    if self.enqueue(other, Some(cid)).is_err() {
                        conflict = Some(cid);
                    }
                } else {
                    conflict = Some(cid);
                }
                if conflict.is_some() {
                    // 尚未处理的表项原样保留，避免丢监视。
                    while i < wlist.len() {
                        wlist[j] = wlist[i];
                        j += 1;
                        i += 1;
                    }
                }
            }
            self.watches[wi] = wlist;
            // 截断到保留长度 j（换防的项没有写回）。
            self.watches[wi].truncate(j);

            if let Some(cid) = conflict {
                return Some(cid);
            }
        }
        None
    }

    // ---------- 决策与回退 ----------

    fn decide(&mut self) {
        let v = self
            .order
            .iter()
            .copied()
            .find(|&v| self.value[v] == Val::Unassigned)
            .expect("decision only when an unassigned variable exists");
        self.trail_lim.push(self.trail.len());
        self.decisions += 1;
        // 固定负极性优先（确定性）。
        self.enqueue(-(v as Lit), None)
            .expect("fresh decision cannot conflict");
    }

    /// 回退到 blevel 层：弹出其后全部 trail 文字、撤销赋值，并复位传播队列。
    fn cancel_until(&mut self, blevel: u32) {
        if (self.trail_lim.len() as u32) > blevel {
            let cutoff = self.trail_lim[blevel as usize];
            for &l in &self.trail[cutoff..] {
                let v = l.unsigned_abs() as usize;
                self.value[v] = Val::Unassigned;
                self.level[v] = 0;
                self.reason[v] = None;
            }
            self.trail.truncate(cutoff);
            self.trail_lim.truncate(blevel as usize);
        }
        // 关键：活下来的 trail 文字此前都已传播过，队列应是空尾而不是旧 qhead。
        self.qhead = self.trail.len();
    }

    fn build_model(&self) -> Vec<bool> {
        let mut m = vec![false; self.n + 1];
        for (v, slot) in m.iter_mut().enumerate().skip(1) {
            *slot = self.value[v] == Val::True;
        }
        m
    }

    // ---------- 冲突分析（1-UIP + 消解记录） ----------

    /// 当前层（>0）冲突的标准 1-UIP 分析。
    ///
    /// 返回 (学习子句, 回溯层, 起始子句引用, 消解操作序列)。
    /// 学习子句 lits[0] 是 UIP 断言文字，其余按分析顺序排列。
    fn analyze(
        &self,
        conflict: ClauseId,
        cur_level: u32,
    ) -> (Vec<Lit>, u32, String, Vec<crate::evidence::ResolventOp>) {
        debug_assert!(cur_level > 0);
        let mut seen = vec![false; self.n + 1];
        let mut pathc = 0u32;
        // 当前选中的 trail 文字；0 表示首轮（并入整个冲突子句）。
        let mut p: Lit = 0;
        let mut reason: Option<ClauseId> = Some(conflict);
        let mut learned: Vec<Lit> = Vec::new();
        let mut blevel = 0u32;
        let mut ops = Vec::new();
        let start_ref = self.origins[conflict].as_ref();

        // 从 trail 尾部向前扫描。
        let mut idx = self.trail.len();

        loop {
            if let Some(rc) = reason {
                for &q in &self.db[rc] {
                    if q == p {
                        continue; // reason 子句里被消解的断言文字本身
                    }
                    let v = q.unsigned_abs() as usize;
                    if seen[v] {
                        continue;
                    }
                    seen[v] = true;
                    if self.level[v] >= cur_level {
                        pathc += 1;
                    } else {
                        // 低层（含第 0 层）文字保留在学习子句与消解记录中。
                        // 第 0 层文字在当前部分赋值下恒真，断言它们无害；
                        // 若丢弃，检查器按记录重放消解时会得到不同的子句。
                        learned.push(q);
                        blevel = blevel.max(self.level[v]);
                    }
                }
            }

            // 沿 trail 选出最近的、仍在当前消解式中的当前层文字。
            loop {
                idx -= 1;
                let q = self.trail[idx];
                if seen[q.unsigned_abs() as usize] {
                    p = q;
                    break;
                }
            }
            let v = p.unsigned_abs() as usize;
            seen[v] = false;
            pathc -= 1;
            if pathc == 0 {
                break;
            }
            // p 是传播文字：以其 reason 继续消解，并记录给证明检查器。
            let rc = self.reason[v].expect("non-UIP current-level literal has a reason");
            ops.push(crate::evidence::ResolventOp {
                pivot_var: v as u32,
                with_ref: self.origins[rc].as_ref(),
            });
            reason = Some(rc);
        }

        // UIP 断言文字放在 0 号位。
        learned.insert(0, -p);
        (learned, blevel, start_ref, ops)
    }

    /// 第 0 层冲突分析：沿 reason 链消解，直到当前消解式为空。
    ///
    /// 每次选中消解式中“最近被传播”的文字，与它的理由子句消解；
    /// 产出的每条派生式都记录下来（检查器逐步复核，最终一条为空子句）。
    /// 中间消解式只活在本函数的局部状态里，不回写子句数据库。
    fn analyze_level0(&self, conflict: ClauseId, id_offset: usize) -> Vec<DerivedClause> {
        if self.db[conflict].is_empty() {
            return Vec::new(); // 冲突本身就是空子句（安装期一般已提前返回，这里防御）。
        }

        let mut entries = Vec::new();
        // 当前消解式与它在证明中的引用名（输入子句或此前派生条目）。
        let mut current: Vec<Lit> = self.db[conflict].clone();
        let mut current_ref = self.origins[conflict].as_ref();
        let mut scan_from = self.trail.len();

        loop {
            // 在当前消解式中找最近入 trail 的变量（其理由子句就是消解对手）。
            let mut chosen: Option<(usize, Lit)> = None;
            for i in (0..scan_from).rev() {
                let q = self.trail[i];
                let var = q.unsigned_abs() as usize;
                if current.iter().any(|l| l.unsigned_abs() as usize == var) {
                    chosen = Some((i, q));
                    break;
                }
            }
            let (pos, p) = match chosen {
                Some(c) => c,
                None => break, // 无可消解文字；冲突若真实存在，current 此时为空。
            };
            scan_from = pos;
            let v = p.unsigned_abs() as usize;

            let rc = match self.reason[v] {
                Some(rc) => rc,
                None => break, // 单位输入文字没有理由子句；真实冲突不会在此停止。
            };

            let next = self.resolve_clauses(&current, &self.db[rc], v);
            let id = format!("d{}", id_offset + entries.len());
            entries.push(DerivedClause {
                id: id.clone(),
                literals: self.sorted_snapshot(&next),
                start_ref: current_ref,
                resolvents: vec![crate::evidence::ResolventOp {
                    pivot_var: v as u32,
                    with_ref: self.origins[rc].as_ref(),
                }],
            });

            current = next;
            current_ref = id;
            if current.is_empty() {
                break;
            }
        }
        entries
    }

    /// 两个子句在变量 `pivot` 上的消解结果（去重）。
    fn resolve_clauses(&self, a: &[Lit], b: &[Lit], pivot: usize) -> Vec<Lit> {
        let mut out: Vec<Lit> = Vec::with_capacity(a.len() + b.len());
        for &l in a.iter().chain(b.iter()) {
            if l.unsigned_abs() as usize == pivot {
                continue;
            }
            if !out.contains(&l) {
                out.push(l);
            }
        }
        out.sort_by_key(|l| (l.unsigned_abs(), *l < 0));
        out
    }

    fn sorted_snapshot(&self, lits: &[Lit]) -> Vec<Lit> {
        let mut v = lits.to_vec();
        v.sort_by_key(|l| (l.unsigned_abs(), *l < 0));
        v
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cnf::{normalize_formula, Clause};

    fn solve(raw: &[Vec<Lit>], limits: SolveLimits) -> SolveOutcome {
        let (f, _) = normalize_formula(None, raw).unwrap();
        Solver::new(&f).solve(&limits)
    }

    #[test]
    fn cascading_unit_propagation_needs_no_decisions() {
        // x1, ¬x1∨x2, ¬x2∨x3, ¬x3∨x4 级联推出全 true。
        let out = solve(
            &[vec![1], vec![-1, 2], vec![-2, 3], vec![-3, 4]],
            SolveLimits::default(),
        );
        assert_eq!(out.status, Status::Sat);
        let m = out.model.unwrap();
        assert_eq!(&m[1..=4], &[true, true, true, true]);
        assert_eq!(out.diagnostics.decisions, 0);
    }

    #[test]
    fn contradictory_units_derive_empty_at_level_zero() {
        let out = solve(&[vec![1], vec![-1]], SolveLimits::default());
        assert_eq!(out.status, Status::Unsat);
        let proof = out.proof.unwrap();
        assert_eq!(proof.empty_clause_ref, "d0");
        assert_eq!(proof.derived_clauses.len(), 1);
        assert!(proof.derived_clauses[0].literals.is_empty());
    }

    #[test]
    fn explicit_empty_clause_is_unsat() {
        let out = solve(&[vec![1, 2], vec![]], SolveLimits::default());
        assert_eq!(out.status, Status::Unsat);
        assert!(out.proof.unwrap().derived_clauses.is_empty());
    }

    #[test]
    fn requires_backtracking_to_find_model() {
        // x1 出现 3 次为首决策。固定负分支 x1=false 时，(x1∨x2)∧(x1∨¬x2)
        // 要求 x2 同时为真和假，立即冲突；回退到 x1=true 后，x3 由
        // (¬x1∨x3) 单位传播为真，得到 SAT。
        let out = solve(
            &[vec![1, 2], vec![1, -2], vec![-1, 3]],
            SolveLimits::default(),
        );
        assert_eq!(out.status, Status::Sat);
        let m = out.model.unwrap();
        assert!(m[1] && m[3]); // x1=true、x3=true
        assert!(
            out.diagnostics.conflicts >= 1,
            "expected at least one conflict"
        );
    }

    #[test]
    fn zero_decision_budget_yields_unknown_not_unsat() {
        let out = solve(
            &[vec![1, 2], vec![-1, 2], vec![1, -2]],
            SolveLimits {
                max_decisions: Some(0),
                ..Default::default()
            },
        );
        assert_eq!(out.status, Status::Unknown);
        assert_eq!(out.diagnostics.stop_reason, StopReason::DecisionBudget);
    }

    #[test]
    fn tautologies_are_normalized_away_before_search() {
        // 重言子句丢弃后，公式只有单位 (x1)。
        let (f, notes) = normalize_formula(None, &[vec![1, -1], vec![1]]).unwrap();
        assert_eq!(f.clauses, vec![Clause { lits: vec![1] }]);
        assert_eq!(notes.len(), 1);
        let out = Solver::new(&f).solve(&SolveLimits::default());
        assert_eq!(out.status, Status::Sat);
    }
}
