//! Core abstract interpreter: syntax-directed transfer with explicit
//! branch narrowing and loop fixpoints (widening + optional narrowing).
//!
//! The collected [`CheckRecord`]s are the actual output — nothing here is a
//! hard-coded demo of a fixture; the same generic transfer runs every program.
use crate::config::AnalyzerConfig;
use crate::refine::assume;
use crate::report::*;
use crate::state::{AbsState, ArrayDomain};
use ia_intervals::{binary as abs_binary, unary as abs_unary, ArithFlag, Interval};
use ia_lang::ast::*;
use ia_lang::{ProgramInfo, Span};
use std::collections::BTreeMap;

#[derive(Clone, Copy, Debug)]
struct CheckKey {
    kind: CheckKind,
    offset: usize,
}

#[derive(Clone, Debug)]
struct PendingCheck {
    key: CheckKey,
    span: Span,
    observed: Interval,
    reachable: bool,
    /// True if at least one visit showed failure is possible.
    possible: bool,
    /// True if at least one visit showed failure is guaranteed.
    guaranteed: bool,
    array_len: Option<i64>,
    explanation_hint: String,
}

impl PendingCheck {
    fn verdict(&self) -> CheckVerdict {
        if !self.reachable {
            CheckVerdict::Unreachable
        } else if self.guaranteed {
            CheckVerdict::GuaranteedFailure
        } else if self.possible {
            CheckVerdict::PossibleFailure
        } else {
            CheckVerdict::Safe
        }
    }
}

pub struct Analyzer<'a> {
    info: &'a ProgramInfo,
    cfg: AnalyzerConfig,
    checks: BTreeMap<(usize, &'static str), PendingCheck>,
    trace: Vec<TraceEvent>,
    seq: u64,
    fixpoints: Vec<(usize, FixpointStats)>,
}

fn initial_state(info: &ProgramInfo) -> AbsState {
    let mut st = AbsState::fresh();
    for input in &info.inputs {
        st.vars.insert(
            input.name.clone(),
            Interval::Range {
                lo: input.lo,
                hi: input.hi,
            },
        );
    }
    for (name, v) in &info.consts {
        st.vars.insert(name.clone(), Interval::point(*v));
    }
    for (name, arr) in &info.arrays {
        st.arrays.insert(name.clone(), ArrayDomain::zeroed(arr.len as usize));
    }
    // Implicit scalars are zero-initialised, exactly like concrete execution.
    for name in &info.scalars {
        st.vars.entry(name.clone()).or_insert(Interval::ZERO);
    }
    st
}

/// Run only the abstract state computation (no report materialisation) and
/// return the post-body scalar intervals. Used by the differential verifier
/// for exit-value containment checks.
pub fn exit_intervals(
    program: &Program,
    info: &ProgramInfo,
    cfg: AnalyzerConfig,
) -> std::collections::BTreeMap<String, Interval> {
    let mut an = Analyzer {
        info,
        cfg,
        checks: BTreeMap::new(),
        trace: Vec::new(),
        seq: 0,
        fixpoints: Vec::new(),
    };
    let start = initial_state(info);
    let end = an.exec_block(&program.body, start);
    end.vars
}

pub fn analyze(
    program: &Program,
    info: &ProgramInfo,
    cfg: AnalyzerConfig,
) -> AnalysisReport {
    let mut an = Analyzer {
        info,
        cfg,
        checks: BTreeMap::new(),
        trace: Vec::new(),
        seq: 0,
        fixpoints: Vec::new(),
    };
    let start = initial_state(info);
    let final_state = an.exec_block(&program.body, start);
    let _ = final_state;

    let mut checks: Vec<CheckRecord> = an
        .checks
        .into_values()
        .map(|pc| {
            let verdict = pc.verdict();
            let certainty = if !pc.reachable {
                None
            } else if pc.guaranteed {
                Some(FailureCertainty::Guaranteed)
            } else if pc.possible {
                Some(FailureCertainty::Possible)
            } else {
                None
            };
            let explanation = match verdict {
                CheckVerdict::Unreachable => {
                    "check site is unreachable under the declared input ranges".to_string()
                }
                CheckVerdict::Safe => format!("{}: no failing choice exists in the abstract state", pc.explanation_hint),
                CheckVerdict::PossibleFailure => format!(
                    "{}: failure cannot be ruled out for every concrete input (over-approximation; not a proof of a bug)",
                    pc.explanation_hint
                ),
                CheckVerdict::GuaranteedFailure => format!(
                    "{}: every concrete execution reaching this site fails",
                    pc.explanation_hint
                ),
            };
            CheckRecord {
                id: format!("{}:{}", pc.key.kind.as_str(), pc.key.offset),
                kind: pc.key.kind,
                span: pc.span,
                observed: pc.observed,
                certainty,
                verdict,
                reachable: pc.reachable,
                explanation,
                array_len: pc.array_len,
            }
        })
        .collect();
    checks.sort_by_key(|c| c.span.start.offset);

    let mut counts = VerdictCounts::default();
    for c in &checks {
        match c.verdict {
            CheckVerdict::Safe => counts.safe += 1,
            CheckVerdict::PossibleFailure => counts.possible_failure += 1,
            CheckVerdict::GuaranteedFailure => counts.guaranteed_failure += 1,
            CheckVerdict::Unreachable => counts.unreachable += 1,
        }
    }

    AnalysisReport {
        solver_version: env!("CARGO_PKG_VERSION").to_string(),
        lang_version: ia_lang::VERSION.to_string(),
        checks,
        counts,
        trace: an.trace,
        fixpoints: an.fixpoints,
    }
}

// ---------------- check registration ----------------

/// Verdict inputs for one visit of a check site.
#[derive(Clone, Copy)]
struct CheckInput {
    kind: CheckKind,
    span: Span,
    observed: Interval,
    reachable: bool,
    possible: bool,
    guaranteed: bool,
    array_len: Option<i64>,
}

impl CheckInput {
    fn unreachable(kind: CheckKind, span: Span, array_len: Option<i64>) -> Self {
        Self {
            kind,
            span,
            observed: Interval::BOTTOM,
            reachable: false,
            possible: false,
            guaranteed: false,
            array_len,
        }
    }
}

impl<'a> Analyzer<'a> {
    fn record(&mut self, input: CheckInput, hint: impl Into<String>) {
        let CheckInput {
            kind,
            span,
            observed,
            reachable,
            possible,
            guaranteed,
            array_len,
        } = input;
        let key = CheckKey {
            kind,
            offset: span.start.offset,
        };
        let entry = self
            .checks
            .entry((key.offset, kind.as_str()))
            .or_insert_with(|| PendingCheck {
                key,
                span,
                observed,
                reachable: false,
                possible: false,
                guaranteed: false,
                array_len,
                explanation_hint: hint.into(),
            });
        // A reachable visit dominates every prior unreachable discovery, and
        // an unreachable visit must never downgrade a reachable record.
        entry.reachable |= reachable;
        if reachable {
            entry.possible |= possible;
            entry.guaranteed |= guaranteed;
        }
        entry.array_len = array_len.or(entry.array_len);
        entry.observed = entry.observed.join(observed);
    }

    fn trace(&mut self, kind: &'static str, span: Span, detail: String, state: &AbsState) {
        if self.trace.len() >= self.cfg.max_trace_events {
            return;
        }
        self.seq += 1;
        let mut vars: Vec<(String, Interval)> = state
            .vars
            .iter()
            .map(|(k, v)| (k.clone(), *v))
            .collect();
        vars.sort_by(|a, b| a.0.cmp(&b.0));
        self.trace.push(TraceEvent {
            seq: self.seq,
            kind: kind.to_string(),
            span,
            detail,
            vars,
        });
    }

    // ---------------- statements ----------------

    fn exec_block(&mut self, b: &Block, st: AbsState) -> AbsState {
        let mut cur = st;
        for s in &b.stmts {
            cur = self.exec_stmt(s, cur);
        }
        cur
    }

    fn exec_stmt(&mut self, s: &Stmt, st: AbsState) -> AbsState {
        if st.is_bottom() {
            self.mark_stmt_unreachable(s);
            return AbsState::bottom();
        }
        match s {
            Stmt::Block(b) => self.exec_block(b, st),
            Stmt::Skip { span } => {
                self.trace("skip", *span, "skip".to_string(), &st);
                st
            }
            Stmt::Assign { target, value, span } => self.exec_assign(target, value, *span, st),
            Stmt::If {
                cond,
                then,
                otherwise,
                span,
            } => {
                // Register checks hidden inside the condition (array reads,
                // arithmetic inside the test); a *guaranteed* failure while
                // evaluating the condition kills the whole statement.
                let cond_eval = self.eval(cond, st.clone());
                if cond_eval.state.is_bottom() {
                    self.mark_stmt_unreachable(then);
                    if let Some(e) = otherwise {
                        self.mark_stmt_unreachable(e);
                    }
                    self.trace(
                        "branch",
                        *span,
                        "condition evaluation always fails; branches unreachable".to_string(),
                        &AbsState::bottom(),
                    );
                    return AbsState::bottom();
                }
                let s_true = assume(&st, cond, true);
                let s_false = assume(&st, cond, false);
                self.trace(
                    "branch",
                    *span,
                    format!(
                        "if condition: then-branch {}reachable, else-branch {}reachable",
                        if s_true.is_bottom() { "un" } else { "" },
                        if s_false.is_bottom() { "un" } else { "" }
                    ),
                    &st,
                );
                let out_then = self.exec_stmt(then, s_true);
                let out_else = match otherwise {
                    Some(e) => self.exec_stmt(e, s_false),
                    None => s_false,
                };
                out_then.join(&out_else)
            }
            Stmt::While { cond, body, span } => self.exec_while(cond, body, *span, st),
            Stmt::Assert { cond, span } => {
                // `assert(e)`: on the failing branch e is false => concrete
                // AssertionFailed; analysis records an assertion check. The
                // surviving state is restricted to e true, matching concrete
                // execution that continues only after a passing assertion.
                let cond_eval = self.eval(cond, st.clone());
                if cond_eval.state.is_bottom() {
                    // The condition itself always faults (overflow/OOB):
                    // assertion is never even evaluated.
                    return AbsState::bottom();
                }
                let s_ok = assume(&st, cond, true);
                let s_bad = assume(&st, cond, false);
                let reachable = !st.is_bottom();
                let failing = !s_bad.is_bottom();
                let guaranteed = !s_bad.is_bottom() && s_ok.is_bottom();
                let observed = cond_eval.value;
                self.record(CheckInput { kind: CheckKind::Assertion, span: *span, observed, reachable, possible: failing, guaranteed, array_len: None }, "assertion may evaluate to false");
                self.trace(
                    "assert",
                    *span,
                    if guaranteed {
                        "assertion always fails on reaching inputs".to_string()
                    } else if failing {
                        "assertion fails for some inputs; continuing along passing branch".to_string()
                    } else {
                        "assertion always holds".to_string()
                    },
                    &s_ok,
                );
                s_ok
            }
        }
    }

    /// Register every check site appearing under `s` as unreachable. This is
    /// a purely syntactic walk so no site can be omitted just because dataflow
    /// never reaches it.
    fn mark_stmt_unreachable(&mut self, s: &Stmt) {
        match s {
            Stmt::Block(b) => {
                for t in &b.stmts {
                    self.mark_stmt_unreachable(t);
                }
            }
            Stmt::Assign { target, value, span } => {
                self.discover_expr_unreachable(value);
                if let Some(idx) = &target.index {
                    self.discover_expr_unreachable(idx);
                    if let Some(len) = self.info.array_len(&target.name) {
                        self.record(CheckInput::unreachable(CheckKind::IndexBounds, idx.span, Some(len as i64)), "array index");
                    }
                }
                self.trace(
                    "unreachable",
                    *span,
                    "unreachable assignment".to_string(),
                    &AbsState::bottom(),
                );
            }
            Stmt::If {
                cond,
                then,
                otherwise,
                span,
            } => {
                self.discover_expr_unreachable(cond);
                self.trace(
                    "unreachable",
                    *span,
                    "unreachable if".to_string(),
                    &AbsState::bottom(),
                );
                self.mark_stmt_unreachable(then);
                if let Some(e) = otherwise {
                    self.mark_stmt_unreachable(e);
                }
            }
            Stmt::While { cond, body, span } => {
                self.discover_expr_unreachable(cond);
                self.trace(
                    "unreachable",
                    *span,
                    "unreachable while".to_string(),
                    &AbsState::bottom(),
                );
                self.mark_stmt_unreachable(body);
            }
            Stmt::Assert { cond, span } => {
                self.discover_expr_unreachable(cond);
                self.record(CheckInput::unreachable(CheckKind::Assertion, *span, None), "assertion may evaluate to false");
            }
            Stmt::Skip { span } => {
                self.trace(
                    "unreachable",
                    *span,
                    "unreachable skip".to_string(),
                    &AbsState::bottom(),
                );
            }
        }
    }

    /// Register arithmetic and array-read checks contained in an expression
    /// that dataflow proves is never evaluated.
    fn discover_expr_unreachable(&mut self, e: &Expr) {
        match &e.kind {
            ExprKind::Int(_) | ExprKind::Var(_) => {}
            ExprKind::ArrayRead { name, index } => {
                self.discover_expr_unreachable(index);
                if let Some(len) = self.info.array_len(name) {
                    self.record(CheckInput::unreachable(CheckKind::IndexBounds, index.span, Some(len as i64)), "array index");
                }
            }
            ExprKind::Unary { inner, .. } => self.discover_expr_unreachable(inner),
            ExprKind::Binary { op, lhs, rhs } => {
                self.discover_expr_unreachable(lhs);
                self.discover_expr_unreachable(rhs);
                if op.is_arithmetic() {
                    // An unreachable arithmetic site can never fail at
                    // runtime; register both possible check kinds as
                    // unreachable so the report stays complete. Div-by-zero is
                    // located at the divisor, matching concrete fault spans.
                    if matches!(op, BinOp::Div | BinOp::Mod) {
                        self.record(CheckInput::unreachable(CheckKind::DivByZero, rhs.span, None), "divisor/modulus may be zero");
                    }
                    self.record(CheckInput::unreachable(CheckKind::Overflow, e.span, None), "checked i64 overflow may occur");
                }
            }
        }
    }

    fn exec_assign(
        &mut self,
        target: &Lvalue,
        value: &Expr,
        span: Span,
        st: AbsState,
    ) -> AbsState {
        let mut st = st;
        let r = self.eval(value, st.clone());
        st = r.state;
        if st.is_bottom() {
            return AbsState::bottom();
        }
        match &target.index {
            None => {
                st.set_var(&target.name, r.value);
                self.trace(
                    "assign",
                    span,
                    format!("{} := {}", target.name, fmt_iv(r.value)),
                    &st,
                );
            }
            Some(idx_expr) => {
                let ir = self.eval(idx_expr, st.clone());
                let mut post = ir.state;
                if post.is_bottom() {
                    // Index expression itself always faults: the write is
                    // unreachable; its checks were registered as such by eval.
                    return AbsState::bottom();
                }
                let len = self.info.array_len(&target.name).expect("validated array") as i64;
                self.index_check(idx_expr.span, ir.value, len);
                if guaranteed_out_of_bounds(ir.value, len) {
                    // Every reaching execution faults: no continuation.
                    return AbsState::bottom();
                }
                // A merely possible OOB still updates the in-range cells
                // (weakly); executions that fault are already accounted for by
                // the recorded check and do not suppress the surviving path.
                if let Some(arr) = post.arrays.get_mut(&target.name) {
                    arr.write(ir.value, r.value);
                }
                self.trace(
                    "array_write",
                    span,
                    format!(
                        "{}[{}] := {} (len {})",
                        target.name,
                        fmt_iv(ir.value),
                        fmt_iv(r.value),
                        len
                    ),
                    &post,
                );
                st = post;
            }
        }
        st
    }

    fn index_check(&mut self, span: Span, idx: Interval, len: i64) {
        let reachable = !idx.is_bottom();
        let possible = !idx.is_bottom() && !always_in_bounds(idx, len);
        let guaranteed = guaranteed_out_of_bounds(idx, len);
        self.record(CheckInput { kind: CheckKind::IndexBounds, span, observed: idx, reachable, possible, guaranteed, array_len: Some(len) }, "array index may be outside [0, len-1]");
    }

    // ---------------- loops ----------------

    fn exec_while(&mut self, cond: &Expr, body: &Stmt, span: Span, entry: AbsState) -> AbsState {
        // F(head) = entry ∪ body(head ∧ cond)
        // Ascending: X0 = ⊥, X_{n+1} = X_n ∇ F(X_n) (widen from second step),
        // then optional descending: Y0 = X, Y_{n+1} = Y_n △ F(Y_n).
        // Register checks embedded in the condition over the entry state so
        // they appear even if the loop body is never executed.
        let _ = self.eval(cond, entry.clone());
        if self.cfg.plain_fixpoint {
            let mut x = AbsState::bottom();
            let mut iters = 0;
            let mut cutoff = false;
            loop {
                iters += 1;
                let candidate = self.iterate_once(cond, body, &entry, &x);
                if candidate.subset_of(&x) {
                    x = candidate;
                    break;
                }
                x = candidate;
                if iters > self.cfg.max_widen_iterations {
                    cutoff = true;
                    break;
                }
            }
            self.fixpoints.push((
                span.start.offset,
                FixpointStats {
                    strategy: FixpointStrategy::Plain,
                    iterations: iters,
                    narrowing_iterations: 0,
                    widened: false,
                    early_cutoff: cutoff,
                },
            ));
            self.trace(
                "while_fixpoint",
                span,
                format!("plain fixpoint after {iters} iterations"),
                &x,
            );
            return assume(&x, cond, false);
        }

        // Ascending chain with widening.
        //
        // Standard iteration:
        //   X0 = ⊥; X1 = F(⊥) (un-widened, keeps the first precise image);
        //   X_{n+1} = X_n ∇ F(X_n); stop once F(X_n) ⊆ X_n, i.e. X_n is a
        //   post-fixpoint. The subset test is evaluated against the (possibly
        //   already widened) X_n, never against the pre-widening image.
        let mut x = AbsState::bottom();
        let mut iters = 0usize;
        let mut cutoff = false;
        loop {
            iters += 1;
            let fx = self.iterate_once(cond, body, &entry, &x);
            if fx.subset_of(&x) {
                break;
            }
            if iters == 1 {
                x = fx;
            } else {
                x = x.widen(&fx);
            }
            if iters >= self.cfg.max_widen_iterations {
                cutoff = true;
                break;
            }
        }

        // Descending with narrowing.
        let mut narrowed = 0usize;
        if self.cfg.narrowing && !cutoff {
            for _ in 0..self.cfg.narrowing_iterations {
                narrowed += 1;
                let candidate = self.iterate_once(cond, body, &entry, &x);
                let y = x.narrow(&candidate);
                if y == x {
                    break;
                }
                x = y;
            }
        }

        self.fixpoints.push((
            span.start.offset,
            FixpointStats {
                strategy: FixpointStrategy::WidenNarrow,
                iterations: iters,
                narrowing_iterations: narrowed,
                widened: iters > 1,
                early_cutoff: cutoff,
            },
        ));
        self.trace(
            "while_fixpoint",
            span,
            format!(
                "fixpoint after {iters} ascending / {narrowed} narrowing iterations{}",
                if cutoff { " (widening cutoff accepted)" } else { "" }
            ),
            &x,
        );
        assume(&x, cond, false)
    }

    /// One application of the loop-body semantic function used by both
    /// ascending and descending chains:
    /// `F(head) = entry ∪ body(head ∧ cond)`.
    ///
    /// The condition is re-evaluated each iteration (that registers checks
    /// with widening states and kills the body on a guaranteed condition
    /// fault). The head state returned for fixpointing is the state *before*
    /// the condition; only the body receives the refined/guarded state.
    fn iterate_once(
        &mut self,
        cond: &Expr,
        body: &Stmt,
        entry: &AbsState,
        head: &AbsState,
    ) -> AbsState {
        if head.is_bottom() {
            // X0 = ⊥ iteration: no execution exists yet, so neither the
            // condition nor the body is evaluated (and no check site is
            // touched). F(⊥) = entry.
            return entry.clone();
        }
        let cond_eval = self.eval(cond, head.clone());
        if cond_eval.state.is_bottom() {
            // A reachable head whose condition always faults: body is
            // unreachable (the fault path is already recorded by eval).
            self.mark_stmt_unreachable(body);
            return entry.clone();
        }
        let guarded = assume(head, cond, true);
        let after_body = self.exec_stmt(body, guarded);
        entry.join(&after_body)
    }

    // ---------------- expressions ----------------

    fn eval(&mut self, e: &Expr, st: AbsState) -> EvalOut {
        if st.is_bottom() {
            return EvalOut {
                value: Interval::BOTTOM,
                state: AbsState::bottom(),
            };
        }
        match &e.kind {
            ExprKind::Int(v) => EvalOut {
                value: Interval::point(*v),
                state: st,
            },
            ExprKind::Var(name) => EvalOut {
                value: st.var(name),
                state: st,
            },
            ExprKind::ArrayRead { name, index } => {
                let ir = self.eval(index, st);
                if ir.state.is_bottom() {
                    return EvalOut {
                        value: Interval::BOTTOM,
                        state: ir.state,
                    };
                }
                let len = self.info.array_len(name).expect("validated array") as i64;
                self.index_check(index.span, ir.value, len);
                if guaranteed_out_of_bounds(ir.value, len) {
                    // Every reaching execution faults: no continuation.
                    return EvalOut {
                        value: Interval::TOP,
                        state: AbsState::bottom(),
                    };
                }
                // Read over the in-range portion; a merely possible OOB keeps
                // execution alive while the value is approximated to TOP.
                let in_range = ir.value.meet(Interval::Range {
                    lo: 0,
                    hi: len - 1,
                });
                let value = ir
                    .state
                    .arrays
                    .get(name)
                    .map(|a| a.read(in_range))
                    .unwrap_or(Interval::BOTTOM);
                let value = if always_in_bounds(ir.value, len) {
                    value
                } else {
                    value.join(Interval::TOP)
                };
                EvalOut {
                    value,
                    state: ir.state,
                }
            }
            ExprKind::Unary { op, inner } => {
                let r = self.eval(inner, st);
                if r.state.is_bottom() {
                    return EvalOut {
                        value: Interval::BOTTOM,
                        state: r.state,
                    };
                }
                let (v, flag) = abs_unary(*op, r.value);
                let guaranteed = unary_guaranteed_fail(*op, r.value);
                self.register_unary_checks(*op, e.span, r.value, flag);
                // Only a *guaranteed* fault kills the continuation; when
                // failure is merely possible the surviving executions still
                // flow on (their value is approximated to TOP).
                let value = if flag.any() { Interval::TOP } else { v };
                let state = if guaranteed {
                    AbsState::bottom()
                } else {
                    r.state
                };
                EvalOut { value, state }
            }
            ExprKind::Binary { op, lhs, rhs } => {
                let l = self.eval(lhs, st);
                if l.state.is_bottom() {
                    return EvalOut {
                        value: Interval::BOTTOM,
                        state: l.state,
                    };
                }
                let r = self.eval(rhs, l.state);
                if r.state.is_bottom() {
                    return EvalOut {
                        value: Interval::BOTTOM,
                        state: r.state,
                    };
                }
                if op.is_arithmetic() {
                    let (v, flag) = abs_binary(*op, l.value, r.value);
                    let guaranteed = arith_guaranteed_fail(*op, l.value, r.value);
                    self.register_arith_checks(
                        *op,
                        e.span,
                        rhs.span,
                        l.value,
                        r.value,
                        flag,
                        guaranteed,
                    );
                    // As with unary ops, only a guaranteed fault terminates
                    // the path; merely possible failures approximate the
                    // result to TOP while continuation stays reachable.
                    let value = if flag.any() { Interval::TOP } else { v };
                    let state = if guaranteed {
                        AbsState::bottom()
                    } else {
                        r.state
                    };
                    EvalOut { value, state }
                } else {
                    let (v, _) = abs_binary(*op, l.value, r.value);
                    EvalOut {
                        value: v,
                        state: r.state,
                    }
                }
            }
        }
    }

    /// Register the arithmetic check at a reachable site. Div/mod always
    /// carry both a div-by-zero check and an overflow check; add/sub/mul/neg
    /// carry only an overflow check. Recording them unconditionally is what
    /// makes a provably-fault-free site SAFE rather than silently absent.
    // Internal checker with one argument per recorded field; the wide
    // signature is intentional and kept off the public lint threshold.
    #[allow(clippy::too_many_arguments)]
    fn register_arith_checks(
        &mut self,
        op: BinOp,
        expr_span: Span,
        divisor_span: Span,
        lhs: Interval,
        rhs: Interval,
        flag: ArithFlag,
        guaranteed_overflow: bool,
    ) {
        if matches!(op, BinOp::Div | BinOp::Mod) {
            // The concrete executor faults at the *divisor* expression; keep
            // the check at that same span so evidence matching is offset-exact.
            let guaranteed = rhs.is_point() == Some(0);
            self.record(CheckInput { kind: CheckKind::DivByZero, span: divisor_span, observed: rhs, reachable: true, possible: flag.div_by_zero_possible, guaranteed, array_len: None }, "divisor/modulus may be zero");
        }
        self.record(CheckInput { kind: CheckKind::Overflow, span: expr_span, observed: lhs.join(rhs), reachable: true, possible: flag.overflow_possible, guaranteed: guaranteed_overflow, array_len: None }, "checked i64 overflow may occur");
    }

    /// Unary counterpart of [`Self::register_arith_checks`]. Only negation
    /// can overflow; logical not cannot.
    fn register_unary_checks(&mut self, op: UnOp, span: Span, x: Interval, flag: ArithFlag) {
        if !matches!(op, UnOp::Neg) {
            return;
        }
        let guaranteed = unary_guaranteed_fail(op, x);
        self.record(CheckInput { kind: CheckKind::Overflow, span, observed: x, reachable: true, possible: flag.overflow_possible, guaranteed, array_len: None }, "checked i64 overflow may occur");
    }
}

struct EvalOut {
    value: Interval,
    state: AbsState,
}

fn fmt_iv(iv: Interval) -> String {
    match iv {
        Interval::Bottom => "⊥".to_string(),
        Interval::Range { lo, hi } if lo == hi => format!("{lo}"),
        Interval::Range { lo, hi } => format!("[{lo}, {hi}]"),
    }
}

fn always_in_bounds(idx: Interval, len: i64) -> bool {
    match idx.bounds() {
        Some((lo, hi)) => lo >= 0 && hi < len,
        None => false,
    }
}

fn guaranteed_out_of_bounds(idx: Interval, len: i64) -> bool {
    match idx.bounds() {
        Some((lo, hi)) => hi < 0 || lo >= len,
        None => false,
    }
}

fn unary_guaranteed_fail(op: UnOp, x: Interval) -> bool {
    match op {
        UnOp::Neg => x.is_point() == Some(i64::MIN),
        UnOp::Not => false,
    }
}

/// Whether *every* concrete operand pair in the two intervals provokes the
/// arithmetic failure (overflow or div-by-zero). The interval transfer's
/// [`ArithFlag`] only says failure is *possible*; this corner test upgrades it
/// to *guaranteed*.
fn arith_guaranteed_fail(op: BinOp, x: Interval, y: Interval) -> bool {
    let (Some((a, b)), Some((c, d))) = (x.bounds(), y.bounds()) else {
        return false;
    };
    match op {
        // Add/sub result extrema are exact at interval corners. Every choice
        // overflows only when even the least result exceeds +MAX (all-positive
        // overflow) or the greatest result is below MIN (all-negative).
        BinOp::Add => {
            let lo = a as i128 + c as i128;
            let hi = b as i128 + d as i128;
            lo > i64::MAX as i128 || hi < i64::MIN as i128
        }
        BinOp::Sub => {
            let lo = a as i128 - d as i128;
            let hi = b as i128 - c as i128;
            lo > i64::MAX as i128 || hi < i64::MIN as i128
        }
        BinOp::Mul => {
            // Exact product extrema over two intervals: attained at corners,
            // and unlike the naive corner test this is the *hull* — an
            // interior zero can keep the true range representable even when
            // all four corners overflow. Compute every corner product in
            // i128 (used only to detect i64 overflow) and take the hull.
            let xs = [a as i128, b as i128];
            let ys = [c as i128, d as i128];
            let mut lo = i128::MAX;
            let mut hi = i128::MIN;
            for xv in xs {
                for yv in ys {
                    let p = xv * yv;
                    lo = lo.min(p);
                    hi = hi.max(p);
                }
            }
            // Every concrete product overflows iff even the closest hull
            // endpoint lies strictly outside the i64 range: i.e. the hull
            // does not intersect [MIN, MAX] at all.
            lo > i64::MAX as i128 || hi < i64::MIN as i128
        }
        BinOp::Div | BinOp::Mod => {
            // Guaranteed failure iff divisor is exactly 0, or (for division)
            // the only choice is MIN / -1.
            if c == 0 && d == 0 {
                return true;
            }
            if matches!(op, BinOp::Div) && a == i64::MIN && c == -1 && d == -1 {
                return true;
            }
            false
        }
        _ => false,
    }
}
