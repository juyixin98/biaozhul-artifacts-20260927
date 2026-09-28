//! Solver kernel: abstract transfer functions over [`AbsState`], worklist-free
//! chaotic iteration with widening at loop heads, optional narrowing, and a
//! separate reporting pass that emits checks once the final invariants are
//! known.

use crate::config::Config;
use crate::kernel::interval::Interval;
use crate::kernel::state::{AbsState, ArrayAbs};
use crate::lang::error::LangError;
use crate::lang::{Cond, Expr, Span, Stmt};
use crate::report::{
    verdict_of, AnalysisReport, CheckKind, CheckRecord, Evidence, LoopInvariant, Observation,
    TraceEvent, ANALYZER_VERSION,
};
use std::collections::{BTreeMap, BTreeSet};

enum Mode {
    /// Computes loop invariants; records trace events but no checks.
    Solve,
    /// Emits checks/observations using pre-computed loop invariants.
    Report,
}

pub struct Analyzer {
    cfg: Config,
    mode: Mode,
    invariants: BTreeMap<u32, LoopInvariant>,
    checks: Vec<CheckRecord>,
    observations: Vec<Observation>,
    trace: Vec<TraceEvent>,
}

impl Analyzer {
    fn new(cfg: Config, mode: Mode) -> Self {
        Analyzer {
            cfg,
            mode,
            invariants: BTreeMap::new(),
            checks: Vec::new(),
            observations: Vec::new(),
            trace: Vec::new(),
        }
    }

    /// Silent analyzer used by the evidence checker for one-step refinement.
    pub(crate) fn silent_runner(cfg: &Config) -> Self {
        Analyzer::new(cfg.clone(), Mode::Solve)
    }

    fn recording(&self) -> bool {
        matches!(self.mode, Mode::Report)
    }

    /// Full pipeline: validate, solve fixpoints, report checks.
    pub fn analyze(
        program: &[Stmt],
        cfg: Config,
        program_hash: String,
    ) -> Result<AnalysisReport, LangError> {
        validate(program)?;

        let mut solver = Analyzer::solve_runner(cfg.clone());
        let _solve_exit = solver.exec_block(program, AbsState::new());

        let report = solver.into_report(program, program_hash)?;
        Ok(report)
    }

    /// Run the reporting pass using externally supplied invariants (used by the
    /// evidence checker, which feeds back the invariants embedded in a report).
    pub fn report_with_invariants(
        program: &[Stmt],
        cfg: Config,
        invariants: BTreeMap<u32, LoopInvariant>,
    ) -> (Vec<CheckRecord>, Vec<Observation>, AbsState) {
        let mut reporter = Analyzer::new(cfg, Mode::Report);
        reporter.invariants = invariants;
        let exit_state = reporter.exec_block(program, AbsState::new());

        let mut seen: BTreeSet<(CheckKind, u32)> = BTreeSet::new();
        reporter
            .checks
            .retain(|c| seen.insert((c.kind, c.span.offset)));
        reporter
            .checks
            .sort_by_key(|c| (c.span.offset, c.span.line, c.kind));
        reporter.observations.sort_by_key(|o| match o {
            Observation::AssignValue { span, .. } => span.offset,
        });
        for (id, c) in reporter.checks.iter_mut().enumerate() {
            c.id = id;
        }
        (reporter.checks, reporter.observations, exit_state)
    }

    /// One silent (check-free) transfer execution of a statement block, used by
    /// the evidence checker to test whether supplied invariants are inductive.
    pub fn silent_exec(
        cfg: &Config,
        stmts: &[Stmt],
        st: AbsState,
    ) -> (AbsState, BTreeMap<u32, LoopInvariant>) {
        let mut a = Analyzer::new(cfg.clone(), Mode::Solve);
        let out = a.exec_block(stmts, st);
        (out, a.invariants)
    }

    pub fn refine(&mut self, st: &AbsState, cond: &Cond, polarity: bool) -> AbsState {
        self.refine_cond(st, cond, polarity)
    }

    fn solve_runner(cfg: Config) -> Self {
        Analyzer::new(cfg, Mode::Solve)
    }

    fn into_report(
        mut self,
        program: &[Stmt],
        program_hash: String,
    ) -> Result<AnalysisReport, LangError> {
        let cfg = self.cfg.clone();
        let supplied: BTreeMap<u32, LoopInvariant> = self.invariants.clone();
        let (checks, observations, exit_state) =
            Analyzer::report_with_invariants(program, cfg.clone(), supplied);

        let mut loop_invariants: Vec<LoopInvariant> = self.invariants.values().cloned().collect();
        loop_invariants.sort_by_key(|i| i.span.offset);

        let trace = std::mem::take(&mut self.trace);
        let mut report = AnalysisReport {
            analyzer_version: ANALYZER_VERSION.to_string(),
            program_hash,
            config: cfg,
            checks,
            observations,
            loop_invariants,
            exit_state,
            trace,
            summary: Default::default(),
        };
        report.summarize();
        Ok(report)
    }

    // ---------- statement execution ----------

    fn exec_block(&mut self, stmts: &[Stmt], mut st: AbsState) -> AbsState {
        for s in stmts {
            if st.is_bottom {
                self.record_unreachable(s);
                continue;
            }
            st = self.exec_stmt(s, st);
        }
        st
    }

    fn exec_stmt(&mut self, s: &Stmt, st: AbsState) -> AbsState {
        match s {
            Stmt::Input { name, lo, hi, .. } => {
                let mut st = st;
                st.set_var(name, Interval::finite(i128::from(*lo), i128::from(*hi)));
                st
            }
            Stmt::ArrayDecl { name, len, .. } => {
                let mut st = st;
                st.arrays.insert(
                    name.clone(),
                    ArrayAbs {
                        len: *len as i64,
                        elem: Interval::constant(0),
                    },
                );
                st
            }
            Stmt::Assign { name, expr, span } => {
                let mut st = st;
                let v = self.eval(expr, &st);
                if self.recording() {
                    self.observations.push(Observation::AssignValue {
                        span: *span,
                        target: name.clone(),
                        interval: v,
                    });
                }
                st.set_var(name, v);
                st
            }
            Stmt::ArrayStore {
                name, index, value, ..
            } => {
                let idx = self.eval(index, &st);
                let val = self.eval(value, &st);
                self.record_index_check(index.span(), idx, st.arrays.get(name));
                let mut st = st;
                if let Some(arr) = st.arrays.get_mut(name) {
                    arr.elem = arr.elem.join(val);
                }
                st
            }
            Stmt::If {
                cond,
                then_body,
                else_body,
                ..
            } => {
                let then_st = self.refine_cond(&st, cond, true);
                let else_st = self.refine_cond(&st, cond, false);
                let then_out = self.exec_block(then_body, then_st);
                let else_out = self.exec_block(else_body, else_st);
                then_out.join(&else_out)
            }
            Stmt::While { cond, body, span } => match self.mode {
                Mode::Solve => self.solve_while(cond, body, st, *span),
                Mode::Report => {
                    let invariant = self
                        .invariants
                        .get(&span.offset)
                        .expect("solve pass must have recorded a loop invariant")
                        .invariant
                        .clone();
                    // One reporting traversal of the body so its checks are
                    // emitted against the stabilised head state.
                    let body_in = self.refine_cond(&invariant, cond, true);
                    if !body_in.is_bottom {
                        self.exec_block(body, body_in);
                    }
                    self.refine_cond(&invariant, cond, false)
                }
            },
            Stmt::Assert { cond, span } => {
                let true_st = self.refine_cond(&st, cond, true);
                let false_st = self.refine_cond(&st, cond, false);
                let ev = Evidence::Assert {
                    true_feasible: !true_st.is_bottom,
                    false_feasible: !false_st.is_bottom,
                };
                self.record(CheckKind::Assert, *span, ev);
                // On a failing assertion concrete execution stops, so the
                // continuation only contains condition-true states.
                true_st
            }
        }
    }

    // ---------- loop fixpoint ----------

    fn solve_while(&mut self, cond: &Cond, body: &[Stmt], head0: AbsState, span: Span) -> AbsState {
        let mut cur = head0.clone();
        let mut iter = 0usize;
        let mut converged = false;

        while iter < self.cfg.max_iterations {
            let body_in = self.refine_cond(&cur, cond, true);
            let body_out = if body_in.is_bottom {
                AbsState::bottom()
            } else {
                self.exec_block(body, body_in)
            };
            let mut nxt = head0.join(&body_out);

            if iter >= self.cfg.widen_delay {
                nxt = cur.widen(&nxt);
                self.trace.push(TraceEvent::Widening {
                    span,
                    iteration: iter,
                });
            }
            self.trace.push(TraceEvent::Iteration {
                span,
                iteration: iter,
                head_state: nxt.clone(),
            });

            if nxt.subset_of(&cur) {
                cur = nxt;
                converged = true;
                break;
            }
            cur = nxt;
            iter += 1;
        }

        if !converged {
            // Widening always reaches a fixpoint in finite height, so hitting
            // the cap means the cap is misconfigured; the current state is
            // still a sound (wide) approximation.
            self.trace.push(TraceEvent::FixpointIterationCap {
                span,
                max_iterations: self.cfg.max_iterations,
            });
        }
        self.trace.push(TraceEvent::Converged {
            span,
            iterations: iter,
        });

        let mut narrowing_passes = 0usize;
        if self.cfg.enable_narrowing {
            for pass in 0..self.cfg.narrow_iters {
                let body_in = self.refine_cond(&cur, cond, true);
                let body_out = if body_in.is_bottom {
                    AbsState::bottom()
                } else {
                    self.exec_block(body, body_in)
                };
                let nxt = cur.narrow(&head0.join(&body_out));
                narrowing_passes += 1;
                self.trace.push(TraceEvent::NarrowingPass {
                    span,
                    pass: pass + 1,
                });
                if nxt == cur {
                    break;
                }
                cur = nxt;
            }
        }

        self.invariants.insert(
            span.offset,
            LoopInvariant {
                span,
                pre_state: head0,
                invariant: cur.clone(),
            },
        );
        self.trace.push(TraceEvent::FixpointStabilized {
            span,
            iterations: iter,
            narrowing_passes,
        });
        self.refine_cond(&cur, cond, false)
    }

    // ---------- expression evaluation ----------

    fn eval(&mut self, e: &Expr, st: &AbsState) -> Interval {
        if st.is_bottom {
            return Interval::Bottom;
        }
        match e {
            Expr::Int(v, _) => Interval::constant(i128::from(*v)),
            Expr::Var(name, _) => st.get_var(name),
            Expr::Load { array, index, span } => {
                let idx = self.eval(index, st);
                let arr = st.arrays.get(array);
                self.record_index_check(*span, idx, arr);
                arr.map(|a| a.elem).unwrap_or(Interval::Bottom)
            }
            Expr::Neg(inner, span) => {
                let v = self.eval(inner, st);
                let m = -v;
                self.record_overflow(*span, m);
                m.clamp_to_i64().0
            }
            Expr::Add(l, r, span) => {
                let (a, b) = (self.eval(l, st), self.eval(r, st));
                let m = a + b;
                self.record_overflow(*span, m);
                m.clamp_to_i64().0
            }
            Expr::Sub(l, r, span) => {
                let (a, b) = (self.eval(l, st), self.eval(r, st));
                let m = a - b;
                self.record_overflow(*span, m);
                m.clamp_to_i64().0
            }
            Expr::Mul(l, r, span) => {
                let (a, b) = (self.eval(l, st), self.eval(r, st));
                let m = a * b;
                self.record_overflow(*span, m);
                m.clamp_to_i64().0
            }
        }
    }

    fn record_overflow(&mut self, span: Span, math: Interval) {
        self.record(
            CheckKind::Overflow,
            span,
            Evidence::Overflow {
                result: math,
                i64_lo: i64::MIN,
                i64_hi: i64::MAX,
            },
        );
    }

    fn record_index_check(&mut self, span: Span, idx: Interval, arr: Option<&ArrayAbs>) {
        if let Some(arr) = arr {
            self.record(
                CheckKind::ArrayIndex,
                span,
                Evidence::ArrayIndex {
                    index: idx,
                    valid_lo: 0,
                    valid_hi: arr.len - 1,
                    array_len: arr.len,
                },
            );
        }
    }

    fn record(&mut self, kind: CheckKind, span: Span, evidence: Evidence) {
        if !self.recording() {
            return;
        }
        let verdict = verdict_of(kind, &evidence);
        let detail = match &evidence {
            Evidence::ArrayIndex { index, valid_hi, array_len, .. } => match verdict {
                crate::report::Verdict::Safe => {
                    format!("index {index} always lies in [0, {valid_hi}] (len {array_len})")
                }
                crate::report::Verdict::MaybeViolated => format!(
                    "index {index} may leave [0, {valid_hi}] (len {array_len}); possible out-of-bounds, not proven"
                ),
                crate::report::Verdict::Violated => {
                    format!("index {index} always outside [0, {valid_hi}] (len {array_len}); definite out-of-bounds")
                }
                crate::report::Verdict::Unreachable => "unreachable".into(),
            },
            Evidence::Overflow { result, i64_lo, i64_hi } => match verdict {
                crate::report::Verdict::Safe => {
                    format!("result {result} inside i64 [{i64_lo}, {i64_hi}]")
                }
                crate::report::Verdict::MaybeViolated => format!(
                    "result {result} may leave i64 [{i64_lo}, {i64_hi}]; possible overflow, not proven"
                ),
                crate::report::Verdict::Violated => {
                    format!("result {result} always outside i64 [{i64_lo}, {i64_hi}]; definite overflow")
                }
                crate::report::Verdict::Unreachable => "unreachable".into(),
            },
            Evidence::Assert { true_feasible, false_feasible } => match (*true_feasible, *false_feasible) {
                (true, false) => "assertion always holds".into(),
                (false, true) => "assertion always fails".into(),
                (true, true) => "assertion may fail on some executions; not proven".into(),
                (false, false) => "assertion point unreachable".into(),
            },
            Evidence::Unreachable => "statement is unreachable".into(),
        };
        self.checks.push(CheckRecord {
            id: 0,
            kind,
            span,
            verdict,
            detail,
            evidence,
        });
    }

    fn record_unreachable(&mut self, s: &Stmt) {
        if !self.recording() {
            return;
        }
        let span = s.span();
        self.checks.push(CheckRecord {
            id: 0,
            kind: CheckKind::UnreachableStmt,
            span,
            verdict: crate::report::Verdict::Unreachable,
            detail: format!("unreachable {}", stmt_kind_name(s)),
            evidence: Evidence::Unreachable,
        });
    }

    // ---------- condition refinement ----------

    fn refine_cond(&mut self, st: &AbsState, c: &Cond, polarity: bool) -> AbsState {
        if st.is_bottom {
            return AbsState::bottom();
        }
        match c {
            Cond::Not(inner, _) => self.refine_cond(st, inner, !polarity),
            Cond::And(a, b, _) => {
                if polarity {
                    let t = self.refine_cond(st, a, true);
                    self.refine_cond(&t, b, true)
                } else {
                    // !(a && b) == !a || !b
                    let x = self.refine_cond(st, a, false);
                    let y = self.refine_cond(st, b, false);
                    x.join(&y)
                }
            }
            Cond::Or(a, b, _) => {
                if polarity {
                    let x = self.refine_cond(st, a, true);
                    let y = self.refine_cond(st, b, true);
                    x.join(&y)
                } else {
                    let x = self.refine_cond(st, a, false);
                    self.refine_cond(&x, b, false)
                }
            }
            Cond::Cmp { op, lhs, rhs, .. } => {
                let lv = self.eval(lhs, st);
                let rv = self.eval(rhs, st);
                let effective = if polarity { *op } else { op.negate() };
                let (new_l, new_r) = Interval::refine_cmp(effective, lv, rv);
                let mut out = st.clone();
                if let Expr::Var(name, _) = lhs {
                    out.set_var(name, new_l);
                }
                if let Expr::Var(name, _) = rhs {
                    out.set_var(name, new_r);
                }
                out
            }
        }
    }
}

fn stmt_kind_name(s: &Stmt) -> &'static str {
    match s {
        Stmt::Input { .. } => "input declaration",
        Stmt::ArrayDecl { .. } => "array declaration",
        Stmt::Assign { .. } => "assignment",
        Stmt::ArrayStore { .. } => "array store",
        Stmt::If { .. } => "if statement",
        Stmt::While { .. } => "while statement",
        Stmt::Assert { .. } => "assertion",
    }
}

// ---------- semantic validation ----------

use std::collections::BTreeSet as Set;

/// Declaration/name checks the parser cannot express. Rejects use of
/// undeclared names, scalar/array confusion and duplicate declarations.
pub fn validate(program: &[Stmt]) -> Result<(), LangError> {
    let mut scalars: Set<String> = Set::new();
    let mut arrays: Set<String> = Set::new();
    for s in program {
        validate_stmt(s, &mut scalars, &mut arrays)?;
    }
    Ok(())
}

fn validate_stmt(
    s: &Stmt,
    scalars: &mut Set<String>,
    arrays: &mut Set<String>,
) -> Result<(), LangError> {
    match s {
        Stmt::Input { name, span, .. } => {
            if arrays.contains(name) {
                return Err(LangError::new(
                    format!("`{name}` already declared as an array"),
                    *span,
                ));
            }
            scalars.insert(name.clone());
        }
        Stmt::ArrayDecl { name, span, .. } => {
            if scalars.contains(name) || arrays.contains(name) {
                return Err(LangError::new(
                    format!("`{name}` declared more than once"),
                    *span,
                ));
            }
            arrays.insert(name.clone());
        }
        Stmt::Assign { name, expr, span } => {
            if arrays.contains(name) {
                return Err(LangError::new(
                    format!("cannot assign scalar to array `{name}`; use `{name}[i] := ...`"),
                    *span,
                ));
            }
            validate_expr(expr, scalars, arrays)?;
            scalars.insert(name.clone());
        }
        Stmt::ArrayStore {
            name,
            index,
            value,
            span,
        } => {
            if !arrays.contains(name) {
                return Err(LangError::new(
                    format!("`{name}` is not a declared array"),
                    *span,
                ));
            }
            validate_expr(index, scalars, arrays)?;
            validate_expr(value, scalars, arrays)?;
        }
        Stmt::If {
            cond,
            then_body,
            else_body,
            ..
        } => {
            validate_cond(cond, scalars, arrays)?;
            for t in then_body {
                validate_stmt(t, scalars, arrays)?;
            }
            for e in else_body {
                validate_stmt(e, scalars, arrays)?;
            }
        }
        Stmt::While { cond, body, .. } => {
            validate_cond(cond, scalars, arrays)?;
            for t in body {
                validate_stmt(t, scalars, arrays)?;
            }
        }
        Stmt::Assert { cond, .. } => validate_cond(cond, scalars, arrays)?,
    }
    Ok(())
}

fn validate_expr(e: &Expr, scalars: &Set<String>, arrays: &Set<String>) -> Result<(), LangError> {
    match e {
        Expr::Int(..) => {}
        Expr::Var(name, span) => {
            if arrays.contains(name) {
                return Err(LangError::new(
                    format!("`{name}` is an array; index it with `{name}[i]`"),
                    *span,
                ));
            }
            if !scalars.contains(name) {
                return Err(LangError::new(
                    format!("scalar `{name}` used before declaration"),
                    *span,
                ));
            }
        }
        Expr::Load { array, index, span } => {
            if scalars.contains(array) {
                return Err(LangError::new(
                    format!("`{array}` is a scalar, not an array"),
                    *span,
                ));
            }
            if !arrays.contains(array) {
                return Err(LangError::new(
                    format!("array `{array}` used before declaration"),
                    *span,
                ));
            }
            validate_expr(index, scalars, arrays)?;
        }
        Expr::Neg(x, _) => validate_expr(x, scalars, arrays)?,
        Expr::Add(a, b, _) | Expr::Sub(a, b, _) | Expr::Mul(a, b, _) => {
            validate_expr(a, scalars, arrays)?;
            validate_expr(b, scalars, arrays)?;
        }
    }
    Ok(())
}

fn validate_cond(c: &Cond, scalars: &Set<String>, arrays: &Set<String>) -> Result<(), LangError> {
    match c {
        Cond::Cmp { lhs, rhs, .. } => {
            validate_expr(lhs, scalars, arrays)?;
            validate_expr(rhs, scalars, arrays)?;
        }
        Cond::And(a, b, _) | Cond::Or(a, b, _) => {
            validate_cond(a, scalars, arrays)?;
            validate_cond(b, scalars, arrays)?;
        }
        Cond::Not(a, _) => validate_cond(a, scalars, arrays)?,
    }
    Ok(())
}
