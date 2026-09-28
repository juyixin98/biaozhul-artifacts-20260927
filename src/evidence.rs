//! Evidence verification.
//!
//! Given a program and an [`AnalysisReport`], this module does NOT solve the
//! fixpoint again. Instead it treats the report as a *claim* and independently
//! checks it:
//!
//! 1. every declared loop invariant is inductive: running one silent transfer
//!    iteration from the invariant and joining the pre-loop state stays inside
//!    the claimed invariant (a post-fixpoint check);
//! 2. every check verdict is re-derived from its structured evidence and
//!    re-derived against the supplied invariants by a fresh reporting pass;
//! 3. observations and the exit state match a fresh reporting pass;
//! 4. the source hash, interval well-formedness and trace shape are consistent.
//!
//! A forged or stale report therefore fails verification even though the
//! checker never runs the widening engine itself.

use crate::kernel::solver::Analyzer;
use crate::kernel::state::AbsState;
use crate::lang::{fnv1a64, Cond, Stmt};
use crate::report::{
    verdict_of, AnalysisReport, CheckKind, CheckRecord, LoopInvariant, Observation, TraceEvent,
};
use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct EvidenceFailure {
    pub location: String,
    pub message: String,
}

impl EvidenceFailure {
    fn at(location: impl Into<String>, message: impl Into<String>) -> Self {
        EvidenceFailure {
            location: location.into(),
            message: message.into(),
        }
    }
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct VerificationReport {
    pub ok: bool,
    pub checked_invariants: usize,
    pub checked_checks: usize,
    pub failures: Vec<EvidenceFailure>,
}

pub fn verify(source: &str, program: &[Stmt], report: &AnalysisReport) -> VerificationReport {
    let mut failures = Vec::new();

    // 0. source binding
    let hash = fnv1a64(source);
    if hash != report.program_hash {
        failures.push(EvidenceFailure::at(
            "report.program_hash",
            format!(
                "report hash {} does not match supplied source {}",
                report.program_hash, hash
            ),
        ));
    }

    // 0b. interval well-formedness
    check_state_shape(&mut failures, "exit_state", &report.exit_state);
    for inv in &report.loop_invariants {
        check_state_shape(
            &mut failures,
            &format!("invariant@{}", inv.span),
            &inv.invariant,
        );
        check_state_shape(
            &mut failures,
            &format!("pre_state@{}", inv.span),
            &inv.pre_state,
        );
    }

    // 1. inductiveness of loop invariants
    let mut loops_by_offset: BTreeMap<u32, (&Cond, &[Stmt])> = BTreeMap::new();
    collect_loops(program, &mut loops_by_offset);

    for inv in &report.loop_invariants {
        let key = inv.span.offset;
        let Some((cond, body)) = loops_by_offset.get(&key) else {
            failures.push(EvidenceFailure::at(
                format!("loop_invariant@{}", inv.span),
                "no loop statement in the program has this span",
            ));
            continue;
        };

        // body_in = invariant ∧ cond; one silent iteration re-executes the body,
        // independently recomputing invariants of nested loops as needed.
        let mut step = Analyzer::silent_runner(&report.config);
        let body_in = step.refine(&inv.invariant, cond, true);
        let (body_out, _nested) = Analyzer::silent_exec(&report.config, body, body_in);
        let post = inv.pre_state.join(&body_out);

        if !post.subset_of(&inv.invariant) {
            failures.push(EvidenceFailure::at(
                format!("loop_invariant@{}", inv.span),
                format!(
                    "claimed invariant is not a post-fixpoint: F(X) = {post:?} is not contained in X = {:?}",
                    inv.invariant
                ),
            ));
        }
    }

    let expected_loops = loops_by_offset.len();
    if expected_loops != report.loop_invariants.len() {
        failures.push(EvidenceFailure::at(
            "loop_invariants",
            format!(
                "program contains {expected_loops} loop(s), report carries {} invariant(s)",
                report.loop_invariants.len()
            ),
        ));
    }

    // 2 & 3. re-derive checks / observations / exit state from the supplied
    // invariants without running the solver.
    let supplied: BTreeMap<u32, LoopInvariant> = report
        .loop_invariants
        .iter()
        .map(|i| (i.span.offset, i.clone()))
        .collect();
    let (rechecks, reobs, reexit) =
        Analyzer::report_with_invariants(program, report.config.clone(), supplied);

    if report.checks.len() != rechecks.len() {
        failures.push(EvidenceFailure::at(
            "checks",
            format!(
                "report has {} checks, re-derivation found {}",
                report.checks.len(),
                rechecks.len()
            ),
        ));
    }

    for (i, (claimed, derived)) in report.checks.iter().zip(rechecks.iter()).enumerate() {
        let loc = format!("check[{}]@{}:{}", i, claimed.kind_label(), claimed.span);
        if claimed.span != derived.span || claimed.kind != derived.kind {
            failures.push(EvidenceFailure::at(
                loc.clone(),
                format!(
                    "check identity differs: report has {}:{}, re-derivation has {}:{}",
                    claimed.kind_label(),
                    claimed.span,
                    derived.kind_label(),
                    derived.span
                ),
            ));
        }
        // verdict must follow from its own structured evidence
        let from_evidence = verdict_of(claimed.kind, &claimed.evidence);
        if from_evidence != claimed.verdict {
            failures.push(EvidenceFailure::at(
                loc.clone(),
                format!(
                    "verdict {} does not follow from its evidence (would imply {})",
                    claimed.verdict_label(),
                    from_evidence_label(from_evidence)
                ),
            ));
        }
        if claimed.verdict != derived.verdict {
            failures.push(EvidenceFailure::at(
                loc,
                format!(
                    "verdict {} is not supported by the supplied loop invariants (re-derived {})",
                    claimed.verdict_label(),
                    derived.verdict_label()
                ),
            ));
        }
    }

    if report.observations != reobs {
        let only_report: Vec<&Observation> = report
            .observations
            .iter()
            .filter(|o| !reobs.contains(o))
            .collect();
        let only_derived: Vec<&Observation> = reobs
            .iter()
            .filter(|o| !report.observations.contains(o))
            .collect();
        failures.push(EvidenceFailure::at(
            "observations",
            format!("observations disagree (only in report: {only_report:?}, only re-derived: {only_derived:?})"),
        ));
    }

    if report.exit_state != reexit {
        failures.push(EvidenceFailure::at(
            "exit_state",
            format!(
                "re-derived exit state {reexit:?} does not match reported {:?}",
                report.exit_state
            ),
        ));
    }

    // 4. summary must be internally consistent
    check_summary(report, &mut failures);

    // 5. trace shape: every widening/convergence references a known loop span
    let trace_spans: std::collections::BTreeSet<u32> = loops_by_offset.keys().copied().collect();
    for ev in &report.trace {
        let span = match ev {
            TraceEvent::Iteration { span, .. }
            | TraceEvent::Widening { span, .. }
            | TraceEvent::Converged { span, .. }
            | TraceEvent::NarrowingPass { span, .. }
            | TraceEvent::FixpointStabilized { span, .. }
            | TraceEvent::FixpointIterationCap { span, .. } => span.offset,
        };
        if !trace_spans.contains(&span) {
            failures.push(EvidenceFailure::at(
                "trace",
                format!("trace event references unknown loop at offset {span}"),
            ));
        }
    }

    VerificationReport {
        ok: failures.is_empty(),
        checked_invariants: report.loop_invariants.len(),
        checked_checks: report.checks.len(),
        failures,
    }
}

fn check_state_shape(failures: &mut Vec<EvidenceFailure>, loc: &str, st: &AbsState) {
    if !st.intervals_well_formed() {
        failures.push(EvidenceFailure::at(
            loc,
            "contains an empty-bottom-flagged or malformed interval (lo > hi)",
        ));
    }
}

fn check_summary(report: &AnalysisReport, failures: &mut Vec<EvidenceFailure>) {
    let s = &report.summary;
    let by_kind: usize = s.safe + s.possible_violations + s.definite_violations + s.unreachable;
    if by_kind != s.total_checks || s.total_checks != report.checks.len() {
        failures.push(EvidenceFailure::at(
            "summary",
            format!(
                "summary counts ({by_kind}/{}) do not match {} checks",
                s.total_checks,
                report.checks.len()
            ),
        ));
    }
    for c in &report.checks {
        let in_list = match c.verdict {
            crate::report::Verdict::Safe => true,
            crate::report::Verdict::MaybeViolated => s.possible_violation_ids.contains(&c.id),
            crate::report::Verdict::Violated => s.definite_violation_ids.contains(&c.id),
            crate::report::Verdict::Unreachable => s.unreachable_ids.contains(&c.id),
        };
        if !in_list {
            failures.push(EvidenceFailure::at(
                "summary",
                format!(
                    "check id {} verdict {} missing from summary id lists",
                    c.id, c.verdict
                ),
            ));
        }
    }
}

fn collect_loops<'a>(stmts: &'a [Stmt], out: &mut BTreeMap<u32, (&'a Cond, &'a [Stmt])>) {
    for s in stmts {
        match s {
            Stmt::While { cond, body, span } => {
                out.insert(span.offset, (cond, body));
                collect_loops(body, out);
            }
            Stmt::If {
                then_body,
                else_body,
                ..
            } => {
                collect_loops(then_body, out);
                collect_loops(else_body, out);
            }
            _ => {}
        }
    }
}

impl CheckRecord {
    pub fn kind_label(&self) -> &'static str {
        match self.kind {
            CheckKind::ArrayIndex => "array_index",
            CheckKind::Overflow => "overflow",
            CheckKind::Assert => "assert",
            CheckKind::UnreachableStmt => "unreachable_stmt",
        }
    }

    pub fn verdict_label(&self) -> &'static str {
        from_evidence_label(self.verdict)
    }
}

fn from_evidence_label(v: crate::report::Verdict) -> &'static str {
    match v {
        crate::report::Verdict::Safe => "safe",
        crate::report::Verdict::MaybeViolated => "maybe_violated",
        crate::report::Verdict::Violated => "violated",
        crate::report::Verdict::Unreachable => "unreachable",
    }
}
