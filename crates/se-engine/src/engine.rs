//! Bounded symbolic-execution engine.
//!
//! Design:
//!
//! * Explicit **continuation stack**, so one logical [`PathState`] can be parked while
//!   its sibling is explored — exploration order is deterministic and the path budget
//!   is exact.
//! * Every feasibility question goes to the injected [`SmtSolver`]. Paths are pruned
//!   only on an explicit `unsat`; `unknown` always becomes a recorded cut and forces
//!   the overall verdict to `unknown`.
//! * While loops are unrolled up to `max_loop_unroll` entered iterations per path;
//!   hitting the limit is a cut, never a claim of safety.
//! * Violating inputs are candidates only; independent replay lives in `se-verify`.
//!
//! Every branch side is confirmed `sat` against the path condition before it is pushed
//! or entered, and the permanent constraints include declared input domains. Hence a
//! running path's condition is satisfiable by construction, so guard/assert checks are
//! a single `pc ∧ bad` query: `sat` violates, `unsat` holds on the path, `unknown` is a
//! cut.

use std::collections::BTreeMap;

use se_lang::ast::{Block, Expr, Program, Stmt};
use se_lang::interp::FailureKind;
use se_solver::{
    bv, nonzero, not, rel, CheckResult, CheckStatus, Model, SmtSolver, Term, CmpRel,
};

use crate::report::{
    AnalysisReport, BudgetUse, Cut, CutKind, Evidence, PathRecord, PathStatus,
    StepEvent, Verdict,
};
use crate::symbolic::{Guard, SymbolicEvaluator};

pub const ENGINE_VERSION: &str = concat!("se-engine ", env!("CARGO_PKG_VERSION"));

/// Tunable exploration budget.
#[derive(Clone, Debug)]
pub struct EngineConfig {
    /// Maximum number of path states whose terminals are explored per analysis.
    pub max_paths: usize,
    /// Maximum number of while-loop iterations entered along any single path.
    pub max_loop_unroll: u32,
    /// Clamp every reported model into the declared input domains.
    pub enforce_domains: bool,
    /// Maximum number of retained per-path records and step events.
    pub record_limit: usize,
}

impl Default for EngineConfig {
    fn default() -> Self {
        EngineConfig {
            max_paths: 256,
            max_loop_unroll: 64,
            enforce_domains: true,
            record_limit: 500,
        }
    }
}

/// Continuation frame.
#[derive(Clone)]
enum Frame {
    /// Execute `block[index]`; the remainder was pushed as its own frame first.
    Seq { block: Block, index: usize },
    /// Body finished (or the loop was just entered): re-evaluate the while condition.
    LoopRecheck {
        stmt_id: usize,
        cond: Expr,
        body: Block,
        iterations_seen: u32,
    },
}

#[derive(Clone)]
struct PathState {
    env: BTreeMap<String, Term>,
    pc: Vec<Term>,
    /// Text form of each pc conjunct (audit trail).
    pc_text: Vec<String>,
    stack: Vec<Frame>,
    terminal_stmt: Option<usize>,
    /// Guards emitted by the most recently evaluated expression, in eval order.
    pending_guards: Vec<Guard>,
    /// Set when `finish` classified this path; the driver must stop processing it.
    terminated: bool,
}

impl PathState {
    fn push_block(&mut self, block: Block) {
        if !block.is_empty() {
            self.stack.push(Frame::Seq { block, index: 0 });
        }
    }
}

struct Counters {
    terminals: usize,
    /// Number of path states issued (initial + forks). Bounds the worklist.
    issued: usize,
    forked_branches: usize,
    solver_queries: usize,
    sat: usize,
    unsat: usize,
    unknown: usize,
    max_unroll_observed: u32,
}

pub struct Engine<'a, S: SmtSolver> {
    program: &'a Program,
    solver: &'a S,
    cfg: EngineConfig,
    inputs: Vec<String>,
    counters: Counters,
    paths: Vec<PathRecord>,
    paths_truncated: bool,
    events: Vec<StepEvent>,
    events_truncated: bool,
    cuts: Vec<Cut>,
    evidence: Vec<Evidence>,
    solver_note: Option<String>,
    seq: usize,
    budget_cut_recorded: bool,
    emitter: se_solver::smt::SmtEmitter,
}

/// Result of asking the solver whether a "bad" condition is reachable on a path.
enum Reach {
    /// `pc ∧ bad` is satisfiable; the witness model is attached.
    Reachable(Model),
    /// `pc ∧ bad` is unsatisfiable — the bad event cannot occur on this path.
    Unreachable,
    /// No verdict obtained; the path must be marked unknown.
    Unknown,
}

/// Whether an in-statement fork consumed the current path.
enum ForkFlow {
    Continue,
    Done,
}

impl<'a, S: SmtSolver> Engine<'a, S> {
    pub fn new(program: &'a Program, solver: &'a S, cfg: EngineConfig) -> Self {
        Engine {
            program,
            solver,
            cfg,
            inputs: program.inputs.iter().map(|i| i.name.clone()).collect(),
            counters: Counters {
                terminals: 0,
                issued: 1,
                forked_branches: 0,
                solver_queries: 0,
                sat: 0,
                unsat: 0,
                unknown: 0,
                max_unroll_observed: 0,
            },
            paths: Vec::new(),
            paths_truncated: false,
            events: Vec::new(),
            events_truncated: false,
            cuts: Vec::new(),
            evidence: Vec::new(),
            solver_note: None,
            seq: 0,
            budget_cut_recorded: false,
            emitter: se_solver::smt::SmtEmitter::new(program.width),
        }
    }

    // ------------------------------------------------------------------
    // Top-level driver
    // ------------------------------------------------------------------

    pub fn analyze(mut self) -> AnalysisReport {
        self.log(
            "start",
            None,
            format!(
                "width={} overflow={} inputs={} domain_size={}",
                self.program.width.bits(),
                if self.program.overflow == se_lang::ast::OverflowMode::Wrap {
                    "wrap"
                } else {
                    "trap"
                },
                self.inputs.len(),
                se_lang::interp::domain_size(self.program)
            ),
            Some(format!("{} {}", self.solver.name(), self.solver.version())),
        );

        let mut worklist: Vec<PathState> = vec![self.initial_state()];

        while let Some(mut state) = worklist.pop() {
            if self.counters.terminals >= self.cfg.max_paths {
                self.record_budget_cut(None);
                self.log(
                    "cut",
                    None,
                    format!(
                        "max_paths={} reached; {} state(s) left unexplored",
                        self.cfg.max_paths,
                        worklist.len() + 1
                    ),
                    None,
                );
                break;
            }
            self.run_path(&mut state, &mut worklist);
        }

        let verdict = self.final_verdict();
        self.log(
            "finish",
            None,
            format!(
                "verdict={} terminals={} forks={} queries={} sat={} unsat={} unknown={}",
                verdict.as_str(),
                self.counters.terminals,
                self.counters.forked_branches,
                self.counters.solver_queries,
                self.counters.sat,
                self.counters.unsat,
                self.counters.unknown
            ),
            None,
        );

        AnalysisReport {
            engine_version: ENGINE_VERSION.to_string(),
            verdict: verdict.as_str().to_string(),
            width_bits: self.program.width.bits(),
            overflow: if self.program.overflow == se_lang::ast::OverflowMode::Wrap {
                "wrap"
            } else {
                "trap"
            }
            .to_string(),
            inputs: self.inputs.clone(),
            stmt_count: self.program.stmt_count,
            domain_size: se_lang::interp::domain_size(self.program).to_string(),
            budget: BudgetUse {
                max_paths: self.cfg.max_paths,
                explored_terminals: self.counters.terminals,
                forked_branches: self.counters.forked_branches,
                solver_queries: self.counters.solver_queries,
                sat_queries: self.counters.sat,
                unsat_queries: self.counters.unsat,
                unknown_queries: self.counters.unknown,
                max_loop_unroll: self.cfg.max_loop_unroll,
                max_unroll_observed: self.counters.max_unroll_observed,
            },
            evidence: self.evidence,
            cuts: self.cuts,
            paths: self.paths,
            paths_truncated: self.paths_truncated,
            steps: self.events,
            steps_truncated: self.events_truncated,
            solver_note: self.solver_note,
            error: None,
        }
    }

    fn initial_state(&self) -> PathState {
        let mut env: BTreeMap<String, Term> = BTreeMap::new();
        for var in &self.program.vars {
            env.insert(var.name.clone(), bv(var.init));
        }
        for input in &self.program.inputs {
            env.insert(input.name.clone(), se_solver::var(input.name.clone()));
        }
        let mut pc = Vec::new();
        let mut pc_text = Vec::new();
        if self.cfg.enforce_domains {
            for input in &self.program.inputs {
                if input.low != 0 || input.high != self.program.width.mask_u64() {
                    let v = se_solver::var(input.name.clone());
                    let bounded = se_solver::and(
                        rel(CmpRel::Uge, v.clone(), bv(input.low)),
                        rel(CmpRel::Ule, v, bv(input.high)),
                    );
                    if let Ok(txt) = self.emitter.emit(&bounded) {
                        pc_text.push(txt);
                    }
                    pc.push(bounded);
                }
            }
        }
        let mut state = PathState {
            env,
            pc,
            pc_text,
            stack: Vec::new(),
            terminal_stmt: None,
            pending_guards: Vec::new(),
            terminated: false,
        };
        state.push_block(self.program.body.clone());
        state
    }

    fn final_verdict(&self) -> Verdict {
        if !self.evidence.is_empty() {
            Verdict::Violation
        } else if self.cuts.is_empty() && self.counters.unknown == 0 {
            Verdict::Holds
        } else {
            Verdict::Unknown
        }
    }

    // ------------------------------------------------------------------
    // Per-path execution
    // ------------------------------------------------------------------

    fn run_path(&mut self, state: &mut PathState, worklist: &mut Vec<PathState>) {
        loop {
            // Resolve any guards produced by the previous expression before doing
            // anything else — concrete evaluation fails inside the expression first.
            if !self.drain_guards(state) {
                return;
            }

            let frame = match state.stack.pop() {
                Some(f) => f,
                None => {
                    self.finish(state, PathStatus::Safe);
                    return;
                }
            };

            match frame {
                Frame::Seq { block, index } => {
                    let stmt = block[index].clone();
                    let id = stmt.id();
                    state.terminal_stmt = Some(id);

                    // Continuation after this statement precedes statement effects on
                    // the stack.
                    if index + 1 < block.len() {
                        state.stack.push(Frame::Seq {
                            block: block.clone(),
                            index: index + 1,
                        });
                    }

                    match stmt {
                        Stmt::Assign { target, expr, .. } => {
                            let ev = self.eval(state, &expr, id);
                            state.pending_guards = ev.guards;
                            state.env.insert(target, ev.term);
                        }
                        Stmt::Assume { cond, .. } => {
                            let ev = self.eval(state, &cond, id);
                            state.pending_guards = ev.guards;
                            if !self.drain_guards(state) {
                                return;
                            }
                            let taken = nonzero(ev.term);
                            match self.reachable(state, taken.clone()) {
                                Reach::Reachable(_) => self.add_pc(state, taken),
                                Reach::Unreachable => {
                                    self.finish(state, PathStatus::InfeasibleAssume);
                                    return;
                                }
                                Reach::Unknown => {
                                    self.cut(
                                        state,
                                        CutKind::SolverUnknown,
                                        id,
                                        "assume feasibility unknown",
                                    );
                                    return;
                                }
                            }
                        }
                        Stmt::Assert { cond, .. } => {
                            let ev = self.eval(state, &cond, id);
                            state.pending_guards = ev.guards;
                            if !self.drain_guards(state) {
                                return;
                            }
                            let viol = rel(CmpRel::Eq, ev.term, bv(0));
                            match self.reachable(state, viol) {
                                Reach::Reachable(model) => {
                                    self.record_evidence(
                                        state,
                                        FailureKind::Assertion,
                                        id,
                                        None,
                                        model,
                                    );
                                    self.finish(state, PathStatus::Violation);
                                    return;
                                }
                                Reach::Unreachable => { /* assertion holds on path */ }
                                Reach::Unknown => {
                                    self.cut(
                                        state,
                                        CutKind::SolverUnknown,
                                        id,
                                        "assertion feasibility unknown",
                                    );
                                    return;
                                }
                            }
                        }
                        Stmt::If {
                            cond,
                            then_blk,
                            else_blk,
                            ..
                        } => {
                            let ev = self.eval(state, &cond, id);
                            state.pending_guards = ev.guards;
                            if !self.drain_guards(state) {
                                return;
                            }
                            let taken = nonzero(ev.term.clone());
                            let skipped = not(taken.clone());
                            let yes = self.reachable_status(state, Some(taken.clone()));
                            let no = self.reachable_status(state, Some(skipped.clone()));
                            self.counters.forked_branches += 1;
                            if matches!(
                                self.fork(state, worklist, id, yes, no, taken, skipped, then_blk, else_blk),
                                ForkFlow::Done
                            ) {
                                return;
                            }
                        }
                        Stmt::While { cond, body, .. } => {
                            self.handle_while(state, worklist, id, cond, body, 0);
                            if state.terminated {
                                return;
                            }
                        }
                    }
                }
                Frame::LoopRecheck {
                    stmt_id,
                    cond,
                    body,
                    iterations_seen,
                } => {
                    self.handle_while(state, worklist, stmt_id, cond, body, iterations_seen);
                    if state.terminated {
                        return;
                    }
                }
            }
        }
    }

    /// Process every pending guard in order. Returns false when the path terminated
    /// (violation/infeasible/unknown); true when execution may continue.
    fn drain_guards(&mut self, state: &mut PathState) -> bool {
        let guards = std::mem::take(&mut state.pending_guards);
        for guard in guards {
            let id = guard.stmt_id;
            let bad = not(guard.cond.clone());
            match self.reachable(state, bad) {
                Reach::Reachable(model) => {
                    let kind = guard.kind;
                    let op = guard.op;
                    self.record_evidence(state, kind, id, Some(op), model);
                    self.finish(state, PathStatus::Violation);
                    return false;
                }
                Reach::Unreachable => {} // guard holds on every model of this path
                Reach::Unknown => {
                    self.cut(
                        state,
                        CutKind::SolverUnknown,
                        id,
                        format!(
                            "guard {} ({}) feasibility unknown",
                            kind_str(guard.kind),
                            guard.op
                        ),
                    );
                    return false;
                }
            }
        }
        true
    }

    #[allow(clippy::too_many_arguments)]
    fn fork(
        &mut self,
        state: &mut PathState,
        worklist: &mut Vec<PathState>,
        id: usize,
        yes: CheckStatus,
        no: CheckStatus,
        taken: Term,
        skipped: Term,
        then_blk: Block,
        else_blk: Block,
    ) -> ForkFlow {
        match (yes, no) {
            (CheckStatus::Unsat, CheckStatus::Unsat) => {
                self.finish(state, PathStatus::Infeasible);
                ForkFlow::Done
            }
            (CheckStatus::Unknown, _) | (_, CheckStatus::Unknown) => {
                self.cut(state, CutKind::SolverUnknown, id, "branch feasibility unknown");
                ForkFlow::Done
            }
            (CheckStatus::Sat, CheckStatus::Sat) => {
                // Explore the taken branch inline; park the sibling.
                let mut sibling = state.clone();
                self.add_pc(&mut sibling, skipped.clone());
                sibling.push_block(else_blk);
                self.spawn_sibling(sibling, worklist, id, "else");
                self.add_pc(state, taken);
                state.push_block(then_blk);
                ForkFlow::Continue
            }
            (CheckStatus::Sat, CheckStatus::Unsat) => {
                self.add_pc(state, taken);
                state.push_block(then_blk);
                ForkFlow::Continue
            }
            (CheckStatus::Unsat, CheckStatus::Sat) => {
                self.add_pc(state, skipped);
                state.push_block(else_blk);
                ForkFlow::Continue
            }
        }
    }

    fn handle_while(
        &mut self,
        state: &mut PathState,
        worklist: &mut Vec<PathState>,
        id: usize,
        cond: Expr,
        body: Block,
        iterations_seen: u32,
    ) {
        state.terminal_stmt = Some(id);
        let ev = self.eval(state, &cond, id);
        state.pending_guards = ev.guards;
        if !self.drain_guards(state) {
            return;
        }
        let enter = nonzero(ev.term.clone());
        let exit = not(enter.clone());
        let can_enter = self.reachable_status(state, Some(enter.clone()));
        let can_exit = self.reachable_status(state, Some(exit.clone()));
        self.counters.forked_branches += 1;

        match (can_enter, can_exit) {
            (CheckStatus::Unsat, CheckStatus::Unsat) => {
                self.finish(state, PathStatus::Infeasible);
            }
            (CheckStatus::Unknown, _) | (_, CheckStatus::Unknown) => {
                self.cut(
                    state,
                    CutKind::SolverUnknown,
                    id,
                    format!("loop condition unknown at iteration {iterations_seen}"),
                );
            }
            (CheckStatus::Unsat, CheckStatus::Sat) => {
                // Loop exits now; nothing to push (continuation already scheduled).
            }
            (CheckStatus::Sat, CheckStatus::Unsat) => {
                self.begin_iteration(state, id, cond, body, iterations_seen, enter);
            }
            (CheckStatus::Sat, CheckStatus::Sat) => {
                // Park the exit path, unroll inline.
                let mut sibling = state.clone();
                self.add_pc(&mut sibling, exit.clone());
                self.spawn_sibling(sibling, worklist, id, "loop-exit");
                self.begin_iteration(state, id, cond, body, iterations_seen, enter);
            }
        }
    }

    fn begin_iteration(
        &mut self,
        state: &mut PathState,
        id: usize,
        cond: Expr,
        body: Block,
        iterations_seen: u32,
        enter: Term,
    ) {
        let next = iterations_seen + 1;
        self.counters.max_unroll_observed = self.counters.max_unroll_observed.max(next);
        if next > self.cfg.max_loop_unroll {
            self.cuts.push(Cut {
                kind: CutKind::LoopUnroll,
                stmt_id: Some(id),
                detail: format!(
                    "max_loop_unroll={} reached after {iterations_seen} iterations",
                    self.cfg.max_loop_unroll
                ),
            });
            self.log(
                "cut",
                Some(id),
                format!("loop unroll cap at iteration {iterations_seen}"),
                None,
            );
            self.finish(state, PathStatus::Unknown);
            return;
        }
        self.add_pc(state, enter);
        // Recheck must sit *under* the body on the stack so the body executes first.
        state.stack.push(Frame::LoopRecheck {
            stmt_id: id,
            cond,
            body: body.clone(),
            iterations_seen: next,
        });
        state.push_block(body);
    }

    fn spawn_sibling(
        &mut self,
        sibling: PathState,
        worklist: &mut Vec<PathState>,
        stmt_id: usize,
        label: &str,
    ) {
        if self.counters.issued >= self.cfg.max_paths {
            self.record_budget_cut(Some(stmt_id));
            self.log(
                "cut",
                Some(stmt_id),
                format!("{label} sibling not issued (path budget)"),
                None,
            );
            return;
        }
        self.counters.issued += 1;
        self.log(
            "fork",
            Some(stmt_id),
            format!("issue {label} path (pc={})", sibling.pc.len()),
            None,
        );
        worklist.push(sibling);
    }

    // ------------------------------------------------------------------
    // Solver interaction
    // ------------------------------------------------------------------

    fn query(&mut self, state: &PathState, extra: Option<Term>) -> CheckResult {
        self.counters.solver_queries += 1;
        let mut assumptions = state.pc.clone();
        if let Some(t) = extra {
            assumptions.push(t);
        }
        match self.solver.check(self.program.width, &self.inputs, &assumptions) {
            Ok(r) => {
                match r.status {
                    CheckStatus::Sat => self.counters.sat += 1,
                    CheckStatus::Unsat => self.counters.unsat += 1,
                    CheckStatus::Unknown => {
                        self.counters.unknown += 1;
                        if let Some(note) = &r.reason {
                            self.solver_note = Some(note.clone());
                        }
                    }
                }
                r
            }
            Err(e) => {
                self.counters.unknown += 1;
                self.solver_note = Some(e.to_string());
                CheckResult {
                    status: CheckStatus::Unknown,
                    model: None,
                    reason: Some(e.to_string()),
                    solver: self.solver.name().to_string(),
                    solver_version: self.solver.version().to_string(),
                    elapsed_ms: 0,
                }
            }
        }
    }

    fn reachable(&mut self, state: &PathState, bad: Term) -> Reach {
        let stmt = state.terminal_stmt;
        let r = self.query(state, Some(bad));
        let status = r.status;
        self.log(
            "check",
            stmt,
            match status {
                CheckStatus::Sat => "reachability: sat".into(),
                CheckStatus::Unsat => "reachability: unsat".into(),
                CheckStatus::Unknown => format!(
                    "reachability: unknown ({})",
                    r.reason.unwrap_or_default()
                ),
            },
            Some(r.solver_version),
        );
        match status {
            CheckStatus::Sat => Reach::Reachable(r.model.unwrap_or_default()),
            CheckStatus::Unsat => Reach::Unreachable,
            CheckStatus::Unknown => Reach::Unknown,
        }
    }

    fn reachable_status(
        &mut self,
        state: &PathState,
        cond: Option<Term>,
    ) -> CheckStatus {
        self.query(state, cond).status
    }

    // ------------------------------------------------------------------
    // Bookkeeping
    // ------------------------------------------------------------------

    fn eval(&self, state: &PathState, e: &Expr, stmt_id: usize) -> crate::symbolic::SymEval {
        SymbolicEvaluator {
            program: self.program,
            env: &state.env,
        }
        .eval(e, stmt_id)
    }

    fn add_pc(&self, state: &mut PathState, t: Term) {
        if let Ok(txt) = self.emitter.emit(&t) {
            state.pc_text.push(txt);
        }
        state.pc.push(t);
    }

    fn record_budget_cut(&mut self, stmt_id: Option<usize>) {
        if self.budget_cut_recorded {
            return;
        }
        self.budget_cut_recorded = true;
        self.cuts.push(Cut {
            kind: CutKind::PathBudget,
            stmt_id,
            detail: format!("max_paths={} reached", self.cfg.max_paths),
        });
    }

    fn cut(&mut self, state: &mut PathState, kind: CutKind, stmt_id: usize, detail: impl Into<String>) {
        self.cuts.push(Cut {
            kind,
            stmt_id: Some(stmt_id),
            detail: detail.into(),
        });
        self.finish(state, PathStatus::Unknown);
    }

    fn record_evidence(
        &mut self,
        state: &PathState,
        kind: FailureKind,
        stmt_id: usize,
        op: Option<&'static str>,
        model: Model,
    ) {
        let mut inputs = BTreeMap::new();
        let mask = self.program.width.mask_u64();
        for name in &self.inputs {
            let v = model
                .get(name)
                .copied()
                .or_else(|| self.program.find_input(name).map(|i| i.low))
                .unwrap_or(0);
            inputs.insert(name.clone(), v & mask);
        }
        self.log(
            "violation",
            Some(stmt_id),
            format!(
                "{} candidate via {} model={inputs:?}",
                kind_str(kind),
                op.unwrap_or("-")
            ),
            None,
        );
        self.evidence.push(Evidence {
            failure: kind,
            stmt_id,
            op: op.map(|s| s.to_string()),
            inputs,
            solver: self.solver.name().to_string(),
            solver_version: self.solver.version().to_string(),
            path_condition: state.pc_text.clone(),
        });
    }

    fn finish(&mut self, state: &mut PathState, status: PathStatus) {
        self.counters.terminals += 1;
        if self.paths.len() < self.cfg.record_limit {
            self.paths.push(PathRecord {
                index: self.counters.terminals - 1,
                status: status.as_str().to_string(),
                terminal_stmt: state.terminal_stmt,
                condition: state.pc_text.clone(),
            });
        } else {
            self.paths_truncated = true;
        }
        self.log(
            "terminal",
            state.terminal_stmt,
            format!("path #{} terminal={}", self.counters.terminals, status.as_str()),
            None,
        );
        // A finished path keeps no continuation: prevents handle_while's caller from
        // proceeding after an in-callee termination.
        state.stack.clear();
        state.terminated = true;
    }

    fn log(&mut self, kind: &str, stmt_id: Option<usize>, detail: String, solver: Option<String>) {
        if self.events.len() >= self.cfg.record_limit {
            self.events_truncated = true;
            return;
        }
        self.seq += 1;
        self.events.push(StepEvent {
            seq: self.seq,
            kind: kind.to_string(),
            stmt_id,
            detail,
            solver,
        });
    }
}

fn kind_str(k: FailureKind) -> &'static str {
    k.as_str()
}
