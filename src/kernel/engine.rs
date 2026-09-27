//! Path-exploration engine.
//!
//! Exploration strategy: systematic schedule-based DFS. Every fork point in a
//! run (if/while condition, divisor guard, assertion) is an *event* identified
//! by its ordinal in execution order. A run is steered by a schedule of
//! decisions; when a run makes a free choice it enqueues the sibling schedule
//! (`prefix + [!decision]`). This enumerates every feasible path up to the
//! configured budget without duplicates, and anything left unexplored is
//! reported as truncation — the verdict can then only be `unknown`, never
//! `safe`.

use std::collections::{HashSet, VecDeque};

use z3::ast::Bool;
use z3::{Config, Context, SatResult, Solver};

use crate::config::EngineConfig;
use crate::evidence::concrete::{ConcreteInput, FailureKind as ConcKind};
use crate::evidence::native::{NBool, SsaEnv};
use crate::evidence::replay::replay_counterexample;
use crate::kernel::report::*;
use crate::kernel::state::SymState;
use crate::kernel::translator::{DivisorGuard, Translator};
use crate::lang::ast::*;
use crate::{SERVICE_NAME, SERVICE_VERSION};

/// Hard cap on recorded findings, so a pathological program cannot produce
/// an unbounded report.
const MAX_FINDINGS: usize = 100;

pub struct Engine {
    program: Program,
    cfg: EngineConfig,
    run_id: String,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum EventKind {
    If,
    While,
    /// Divisor-safety guard; `true` decision means "divisor == 0".
    Divisor,
    /// Assertion; `true` decision means "condition holds".
    Assert,
}

impl EventKind {
    /// The side a free choice prefers (the side that continues execution).
    fn continue_side(self) -> bool {
        match self {
            EventKind::If | EventKind::While => true,
            EventKind::Divisor => false,
            EventKind::Assert => true,
        }
    }
}

/// Data captured when a run ends in a failure leaf.
struct FindingData {
    kind: FailureKind,
    node_id: u32,
    line: u32,
    message: Option<String>,
    counterexample: ConcreteInput,
    branches: Vec<PathBranch>,
    pc_native: Vec<NBool>,
    ssa: SsaEnv,
}

enum RunOutcome {
    Leaf {
        result: PathResult,
        finding: Option<FindingData>,
    },
}

/// Outcome of deciding one event; `Taken` carries the asserted literal so a
/// failure leaf can extend the path condition with it.
enum Decision<'ctx> {
    Taken {
        side: bool,
        lit_z3: Bool<'ctx>,
        lit_native: NBool,
    },
    /// The scheduled side is infeasible: stale schedule, drop the run.
    Stale,
    /// Both sides infeasible (contradictory path condition).
    InfeasiblePath,
    /// Solver returned unknown.
    Unknown,
}

pub fn analyze(program: Program, cfg: EngineConfig, run_id: String) -> Result<AnalysisReport, ReplayMismatch> {
    Engine {
        program,
        cfg,
        run_id,
    }
    .run()
}

impl Engine {
    fn run(&mut self) -> Result<AnalysisReport, ReplayMismatch> {
        let mut worklist: VecDeque<Vec<bool>> = VecDeque::new();
        let mut visited: HashSet<Vec<bool>> = HashSet::new();
        worklist.push_back(Vec::new());
        visited.insert(Vec::new());

        let mut paths: Vec<PathResult> = Vec::new();
        let mut findings_raw: Vec<(u32, FindingData)> = Vec::new();
        let mut truncated = false;
        let mut truncation_reason: Option<String> = None;
        let mut runs = 0u32;

        while let Some(schedule) = worklist.pop_front() {
            if runs >= self.cfg.max_paths {
                truncated = true;
                truncation_reason = Some(format!(
                    "path budget {} exhausted with {} schedule(s) left unexplored",
                    self.cfg.max_paths,
                    worklist.len() + 1
                ));
                break;
            }
            runs += 1;
            let mut new_schedules: Vec<Vec<bool>> = Vec::new();
            let outcome = self.run_path(&schedule, &mut new_schedules);
            match outcome {
                RunOutcome::Leaf {
                    mut result,
                    finding,
                } => {
                    result.index = paths.len() as u32;
                    if let Some(f) = finding {
                        if findings_raw.len() < MAX_FINDINGS {
                            findings_raw.push((result.index, f));
                        }
                    }
                    paths.push(result);
                }
            }
            for s in new_schedules {
                if visited.insert(s.clone()) {
                    worklist.push_back(s);
                }
            }
        }

        // Independent replay of every finding; any mismatch is an internal
        // inconsistency and must not be reported as success.
        let mut findings: Vec<FailureFinding> = Vec::new();
        for (path_index, f) in findings_raw.into_iter() {
            let conc_kind: ConcKind = f.kind.into();
            let replay = replay_counterexample(&self.program, conc_kind, f.node_id, &f.counterexample);
            if !replay.reproduced() {
                return Err(ReplayMismatch {
                    run_id: self.run_id.clone(),
                    node_id: f.node_id,
                    detail: format!(
                        "solver reported {:?} at node {} but concrete replay gave {:?}",
                        f.kind, f.node_id, replay.status
                    ),
                });
            }
            findings.push(FailureFinding {
                kind: f.kind,
                node_id: f.node_id,
                line: f.line,
                message: f.message,
                path_index,
                counterexample: f.counterexample.iter().map(|(k, v)| (k.clone(), *v)).collect(),
                path_branches: f.branches,
                replay,
                native_path_condition: f.pc_native,
                native_ssa: f.ssa,
            });
        }
        // Deduplicate identical (kind, node, counterexample) findings.
        findings.sort_by(|a, b| {
            (a.kind as u8, a.node_id, format!("{:?}", a.counterexample))
                .cmp(&(b.kind as u8, b.node_id, format!("{:?}", b.counterexample)))
        });
        findings.dedup_by(|a, b| {
            a.kind == b.kind && a.node_id == b.node_id && a.counterexample == b.counterexample
        });

        let counts = count_paths(&paths, truncated);
        let max_depth = paths.iter().map(|p| p.depth).max().unwrap_or(0);
        let verdict = if !findings.is_empty() {
            Verdict::Unsafe
        } else if counts.unknown > 0 || counts.incomplete_unroll > 0 || truncated {
            Verdict::Unknown
        } else {
            Verdict::Safe
        };

        tracing::info!(
            run_id = %self.run_id,
            verdict = ?verdict,
            paths = paths.len(),
            findings = findings.len(),
            truncated = truncated,
            "analysis finished"
        );

        Ok(AnalysisReport {
            run_id: self.run_id.clone(),
            service_name: SERVICE_NAME.into(),
            service_version: SERVICE_VERSION.into(),
            smt_backend: "z3".into(),
            smt_version: crate::z3_version(),
            verdict,
            engine: EngineConfigEcho::from(&self.cfg),
            budget: BudgetReport {
                paths_explored: runs,
                max_paths: self.cfg.max_paths,
                truncated,
                truncation_reason,
                max_depth,
            },
            path_counts: counts,
            findings,
            paths,
        })
    }

    /// Execute one path under `schedule`; sibling schedules discovered at
    /// free choices are pushed to `out_schedules`.
    fn run_path(&self, schedule: &[bool], out_schedules: &mut Vec<Vec<bool>>) -> RunOutcome {
        let zcfg = Config::new();
        let ctx = Context::new(&zcfg);
        let solver = Solver::new(&ctx);
        let mut params = z3::Params::new(&ctx);
        params.set_u32("timeout", self.cfg.solver_timeout_ms);
        solver.set_params(&params);

        let mut state = SymState::new(&ctx, &self.program);
        let mut translator = Translator::new(&ctx, &mut state, &self.program);
        let mut run = PathRun {
            engine: self,
            solver: &solver,
            schedule,
            cursor: 0,
            decisions: Vec::new(),
            branches: Vec::new(),
            out_schedules,
        };
        let leaf = run.exec_block(&self.program.body, &mut translator);
        let (status, finding) = match leaf {
            LeafKind::Completed => (PathStatus::Safe, None),
            LeafKind::Infeasible => (PathStatus::Infeasible, None),
            LeafKind::Unknown => (PathStatus::UnknownSolver, None),
            LeafKind::UnrollLimit => (PathStatus::IncompleteUnroll, None),
            LeafKind::Failure(data) => (PathStatus::Failed, Some(data)),
        };
        RunOutcome::Leaf {
            result: PathResult {
                index: 0, // assigned by the caller
                status,
                depth: run.branches.len() as u32,
                branches: run.branches,
            },
            finding,
        }
    }
}

enum LeafKind {
    Completed,
    Infeasible,
    Unknown,
    UnrollLimit,
    Failure(FindingData),
}

/// Mutable per-run context.
struct PathRun<'e, 's, 'ctx> {
    engine: &'e Engine,
    solver: &'s Solver<'ctx>,
    schedule: &'e [bool],
    cursor: usize,
    /// Every decision taken so far in *this* run (scheduled or free); the
    /// sibling of a free choice extends this, not the original schedule.
    decisions: Vec<bool>,
    branches: Vec<PathBranch>,
    out_schedules: &'e mut Vec<Vec<bool>>,
}

impl<'e, 's, 'ctx> PathRun<'e, 's, 'ctx> {
    fn exec_block(&mut self, stmts: &[Stmt], t: &mut Translator<'_, 'ctx>) -> LeafKind {
        for s in stmts {
            match self.exec_stmt(s, t) {
                LeafKind::Completed => {}
                other => return other,
            }
        }
        LeafKind::Completed
    }

    fn exec_stmt(&mut self, s: &Stmt, t: &mut Translator<'_, 'ctx>) -> LeafKind {
        match &s.kind {
            StmtKind::Let { name, ty, value } => {
                let mut guards = Vec::new();
                let (z, native) = t.int(value, &mut guards);
                if let Some(leaf) = self.process_guards(&guards, t) {
                    return leaf;
                }
                t.state_declare(name, *ty, z, native);
                LeafKind::Completed
            }
            StmtKind::Assign { name, value } => {
                let mut guards = Vec::new();
                let (z, native) = t.int(value, &mut guards);
                if let Some(leaf) = self.process_guards(&guards, t) {
                    return leaf;
                }
                t.state_assign(name, z, native);
                LeafKind::Completed
            }
            StmtKind::Assume { cond } => {
                let mut guards = Vec::new();
                let (z, n) = t.bool_(cond, &mut guards);
                if let Some(leaf) = self.process_guards(&guards, t) {
                    return leaf;
                }
                // Assume never forks: an infeasible assumption kills the path.
                match self.check(&z) {
                    SatResult::Sat => {
                        self.solver.assert(&z);
                        t.state_add_constraint(z, n);
                        LeafKind::Completed
                    }
                    SatResult::Unsat => LeafKind::Infeasible,
                    SatResult::Unknown => LeafKind::Unknown,
                }
            }
            StmtKind::Assert { cond, message } => {
                let mut guards = Vec::new();
                let (z, n) = t.bool_(cond, &mut guards);
                if let Some(leaf) = self.process_guards(&guards, t) {
                    return leaf;
                }
                match self.decide(z, n, s.id.get(), s.span.line, EventKind::Assert, t) {
                    Decision::Taken { side: true, .. } => LeafKind::Completed,
                    Decision::Taken {
                        side: false,
                        lit_z3,
                        lit_native,
                    } => self.failure_leaf(
                        t,
                        FailureKind::AssertionFailed,
                        s.id.get(),
                        s.span.line,
                        message.clone(),
                        lit_z3,
                        lit_native,
                    ),
                    Decision::Stale | Decision::InfeasiblePath => LeafKind::Infeasible,
                    Decision::Unknown => LeafKind::Unknown,
                }
            }
            StmtKind::If { cond, then, els } => {
                let mut guards = Vec::new();
                let (z, n) = t.bool_(cond, &mut guards);
                if let Some(leaf) = self.process_guards(&guards, t) {
                    return leaf;
                }
                match self.decide(z, n, s.id.get(), s.span.line, EventKind::If, t) {
                    Decision::Taken { side: true, .. } => self.exec_scoped(then, t),
                    Decision::Taken { side: false, .. } => self.exec_scoped(els, t),
                    Decision::Stale | Decision::InfeasiblePath => LeafKind::Infeasible,
                    Decision::Unknown => LeafKind::Unknown,
                }
            }
            StmtKind::While { cond, body } => {
                let mut iter = 0u32;
                loop {
                    if iter >= self.engine.cfg.loop_unroll {
                        return LeafKind::UnrollLimit;
                    }
                    let mut guards = Vec::new();
                    let (z, n) = t.bool_(cond, &mut guards);
                    if let Some(leaf) = self.process_guards(&guards, t) {
                        return leaf;
                    }
                    match self.decide(z, n, s.id.get(), s.span.line, EventKind::While, t) {
                        Decision::Taken { side: true, .. } => {
                            iter += 1;
                            match self.exec_scoped(body, t) {
                                LeafKind::Completed => {}
                                other => return other,
                            }
                        }
                        Decision::Taken { side: false, .. } => return LeafKind::Completed,
                        Decision::Stale | Decision::InfeasiblePath => return LeafKind::Infeasible,
                        Decision::Unknown => return LeafKind::Unknown,
                    }
                }
            }
        }
    }

    fn exec_scoped(&mut self, stmts: &[Stmt], t: &mut Translator<'_, 'ctx>) -> LeafKind {
        t.state_push_block();
        let r = self.exec_block(stmts, t);
        t.state_pop_block();
        r
    }

    /// Process divisor guards collected while translating one expression.
    /// Each guard is a Divisor event: decision `true` = divisor is zero.
    fn process_guards(
        &mut self,
        guards: &[DivisorGuard<'ctx>],
        t: &mut Translator<'_, 'ctx>,
    ) -> Option<LeafKind> {
        for g in guards {
            match self.decide(
                g.zero.clone(),
                g.zero_native.clone(),
                g.divisor_node_id,
                g.divisor_line,
                EventKind::Divisor,
                t,
            ) {
                Decision::Taken { side: false, .. } => continue, // divisor != 0, safe
                Decision::Taken {
                    side: true,
                    lit_z3,
                    lit_native,
                } => {
                    return Some(self.failure_leaf(
                        t,
                        FailureKind::DivisionByZero,
                        g.divisor_node_id,
                        g.divisor_line,
                        None,
                        lit_z3,
                        lit_native,
                    ));
                }
                Decision::Stale | Decision::InfeasiblePath => return Some(LeafKind::Infeasible),
                Decision::Unknown => return Some(LeafKind::Unknown),
            }
        }
        None
    }

    /// Core event decision. See module docs for the schedule discipline.
    fn decide(
        &mut self,
        cond_z3: Bool<'ctx>,
        cond_native: NBool,
        node_id: u32,
        line: u32,
        kind: EventKind,
        t: &mut Translator<'_, 'ctx>,
    ) -> Decision<'ctx> {
        let ordinal = self.cursor;
        self.cursor += 1;

        let lit = |side: bool| -> (Bool<'ctx>, NBool) {
            if side {
                (cond_z3.clone(), cond_native.clone())
            } else {
                (cond_z3.not(), NBool::Not(Box::new(cond_native.clone())))
            }
        };

        if ordinal < self.schedule.len() {
            // Scheduled decision: take it if feasible.
            let side = self.schedule[ordinal];
            let (lz, ln) = lit(side);
            return match self.check(&lz) {
                SatResult::Sat => {
                    self.solver.assert(&lz);
                    self.decisions.push(side);
                    t.state_add_constraint(lz.clone(), ln.clone());
                    self.branches.push(PathBranch {
                        node_id,
                        line,
                        taken: side,
                        feasibility: BranchFeasibility::Feasible,
                    });
                    Decision::Taken {
                        side,
                        lit_z3: lz,
                        lit_native: ln,
                    }
                }
                SatResult::Unsat => Decision::Stale,
                SatResult::Unknown => Decision::Unknown,
            };
        }

        // Free choice: prefer the continuing side, enqueue the sibling.
        let preferred = kind.continue_side();
        let (pz, pn) = lit(preferred);
        match self.check(&pz) {
            SatResult::Sat => {
                self.solver.assert(&pz);
                // Sibling schedule = decisions before this event + flip.
                let mut sibling: Vec<bool> = self.decisions.clone();
                sibling.push(!preferred);
                self.out_schedules.push(sibling);
                self.decisions.push(preferred);
                t.state_add_constraint(pz.clone(), pn.clone());
                self.branches.push(PathBranch {
                    node_id,
                    line,
                    taken: preferred,
                    feasibility: BranchFeasibility::Feasible,
                });
                Decision::Taken {
                    side: preferred,
                    lit_z3: pz,
                    lit_native: pn,
                }
            }
            SatResult::Unsat => {
                // Preferred side infeasible: the other side is forced.
                let (oz, on) = lit(!preferred);
                match self.check(&oz) {
                    SatResult::Sat => {
                        self.solver.assert(&oz);
                        self.decisions.push(!preferred);
                        t.state_add_constraint(oz.clone(), on.clone());
                        self.branches.push(PathBranch {
                            node_id,
                            line,
                            taken: !preferred,
                            feasibility: BranchFeasibility::Infeasible,
                        });
                        Decision::Taken {
                            side: !preferred,
                            lit_z3: oz,
                            lit_native: on,
                        }
                    }
                    SatResult::Unsat => Decision::InfeasiblePath,
                    SatResult::Unknown => Decision::Unknown,
                }
            }
            SatResult::Unknown => Decision::Unknown,
        }
    }

    fn check(&self, lit: &Bool<'ctx>) -> SatResult {
        self.solver.push();
        self.solver.assert(lit);
        let r = self.solver.check();
        self.solver.pop(1);
        r
    }

    /// Build a failure leaf: add the failure-side literal to the path
    /// condition, extract a model, snapshot everything needed downstream.
    fn failure_leaf(
        &mut self,
        t: &mut Translator<'_, 'ctx>,
        kind: FailureKind,
        node_id: u32,
        line: u32,
        message: Option<String>,
        lit_z3: Bool<'ctx>,
        lit_native: NBool,
    ) -> LeafKind {
        // The failure-side literal was already asserted on the solver and
        // recorded in the path condition by `decide`; re-check to obtain a
        // model (the path is satisfiable by construction).
        let _ = lit_z3;
        let _ = lit_native;
        let model = match self.solver.check() {
            SatResult::Sat => self.solver.get_model(),
            _ => None,
        };
        let mut counterexample = ConcreteInput::new();
        if let Some(m) = model {
            for p in &self.engine.program.params {
                let key = format!("{}#0", p.name);
                let bv = z3::ast::BV::new_const(t.ctx(), key, p.ty.bits());
                if let Some(v) = m.eval(&bv, true).and_then(|x| x.as_u64()) {
                    counterexample.insert(p.name.clone(), v & p.ty.mask());
                }
            }
        }
        // Any parameter the model left unconstrained defaults to 0; the
        // concrete replay below is the arbiter of correctness either way.
        for p in &self.engine.program.params {
            counterexample.entry(p.name.clone()).or_insert(0);
        }
        LeafKind::Failure(FindingData {
            kind,
            node_id,
            line,
            message,
            counterexample,
            branches: self.branches.clone(),
            pc_native: t.state_pc_native(),
            ssa: t.state_ssa(),
        })
    }
}

fn count_paths(paths: &[PathResult], truncated: bool) -> PathCounts {
    let mut c = PathCounts {
        safe: 0,
        failed: 0,
        infeasible: 0,
        unknown: 0,
        incomplete_unroll: 0,
        incomplete_budget: 0,
    };
    for p in paths {
        match p.status {
            PathStatus::Safe => c.safe += 1,
            PathStatus::Failed => c.failed += 1,
            PathStatus::Infeasible => c.infeasible += 1,
            PathStatus::UnknownSolver => c.unknown += 1,
            PathStatus::IncompleteUnroll => c.incomplete_unroll += 1,
            PathStatus::IncompleteBudget => c.incomplete_budget += 1,
        }
    }
    if truncated {
        c.incomplete_budget += 1;
    }
    c
}
