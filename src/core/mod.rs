//! Deletion-based subset-minimal UNSAT core extraction.
//!
//! Algorithm (classic deletion-based MUS, per-clause):
//!
//! ```text
//! check the whole input once
//!     SAT      -> there is no core
//!     UNKNOWN  -> no core can be honestly reported
//!     UNSAT    -> continue
//! candidate := all clauses
//! for each clause c in candidate order:
//!     trial := solve(candidate - c)
//!       UNSAT   -> the contradiction survives without c: delete c permanently
//!       SAT     -> c participates in the surviving contradiction: keep it
//!       UNKNOWN -> we cannot decide: conservatively keep c, mark undecided
//! ```
//!
//! ## Subset-minimal vs cardinality-minimal
//!
//! On termination every retained clause has passed a SAT *without-it* trial, which
//! proves that removing **any one** member makes the formula satisfiable: the result
//! is *subset-minimal* (an MUS). Nothing here minimizes the **number** of clauses;
//! another order or solver may return a different, equally valid MUS of different
//! size. Cardinality-minimality (the SMUS problem) is out of scope and never claimed.
//!
//! ## What cancellation and budget preserve
//!
//! On cancel/budget the retained set so far is returned with per-member proof state.
//! Clauses that have already passed an UNSAT trial are permanently gone; clauses
//! kept because of SAT trials carry their witness; clauses merely "not yet tried"
//! are explicitly marked untested, never implied safe.

use std::sync::atomic::AtomicBool;

use serde::{Deserialize, Serialize};

use crate::diag::{DecisionAction, DecisionRecord, Redactor, StopReason};
use crate::evidence::{certify, VerificationReport};
use crate::language::{ClauseId, Formula};
use crate::solver::{SatSolver, SolveLimits, SolveStatus};

/// Order in which clauses are considered for deletion. Different orders legitimately
/// lead to different subset-minimal cores when several overlap.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OrderPolicy {
    /// Caller's input order (default; fully deterministic).
    #[default]
    Input,
    /// Short clauses first — empty/unit contradictions tend to dominate small cores.
    ShortestFirst,
    /// Long clauses first — biased toward deleting loose, broad constraints early.
    LongestFirst,
}

/// Knobs for one extraction run, independent of transport (HTTP/job).
#[derive(Debug, Clone)]
pub struct ExtractionConfig {
    pub order: OrderPolicy,
    /// Total solver calls allowed (inclusive of the initial input check).
    /// None = unlimited.
    pub max_solver_calls: Option<usize>,
    pub solve_limits: SolveLimits,
    /// Run the independent verifier after extraction.
    pub verify: bool,
}

impl Default for ExtractionConfig {
    fn default() -> Self {
        Self {
            order: OrderPolicy::Input,
            max_solver_calls: None,
            solve_limits: SolveLimits::default(),
            verify: true,
        }
    }
}

/// Proof state of one member in the returned candidate.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProofState {
    /// A SAT trial of (candidate - this) succeeded: participation demonstrated.
    SatWitness,
    /// It was kept, but the decisive trial was UNKNOWN: participation unproven.
    UnknownTrial,
    /// The run stopped before this clause was ever tried.
    Untested,
    /// Only possible for singleton cores established by the initial input check.
    InitialCheck,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemberProof {
    pub id: ClauseId,
    pub state: ProofState,
}

/// Categorized failure/stop class for assertions and diagnostics.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    /// Candidate fully traversed and (if requested) independently certified.
    Completed,
    /// Full traversal done, but at least one trial was UNKNOWN; minimality unproven
    /// for some members and verification is necessarily inconclusive.
    CompletedWithUnknownTrials,
    /// Call budget (or per-call decision budget on the initial check) exhausted.
    BudgetExhausted,
    Cancelled,
    InputSat,
    InputUnknown,
}

/// Full, serializable report returned through the API.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExtractionReport {
    pub request_id: String,
    pub outcome: Outcome,
    pub stop_reason: StopReason,
    /// Human-readable, redaction-safe explanation of the stop.
    pub summary: String,
    /// Claimed candidate core (positions follow formula order). May be incomplete on
    /// cancel/budget; always complete on `completed`/`completed_with_unknown_trials`.
    pub core: Vec<ClauseId>,
    pub member_proofs: Vec<MemberProof>,
    /// Independent evidence; present when verification was performed.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub verification: Option<VerificationReport>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub verification_skipped_reason: Option<String>,
    /// Ordered, auditable deletion/retention decisions.
    pub decisions: Vec<DecisionRecord>,
    pub solver_calls: usize,
    pub solver_decisions: u64,
    pub budget_limit: Option<usize>,
    /// Candidate size / input size — cardinality is reported, never minimized for.
    pub input_clause_count: usize,
    pub core_clause_count: usize,
    pub fingerprint: String,
    pub solver: String,
    #[serde(default)]
    pub warnings: Vec<String>,
    /// True when the cancellation flag was actually observed during solving at any
    /// point (including during the initial check, where the outcome is input_unknown
    /// rather than "cancelled"). Lets the transport distinguish a cancel-interrupted
    /// run from a naturally-occurring unknown.
    pub cancellation_observed: bool,
}

struct RunState<'a> {
    formula: &'a Formula,
    order_positions: Vec<usize>,
    /// Current candidate by position membership.
    kept: Vec<bool>,
    proofs: Vec<ProofState>,
    decisions: Vec<DecisionRecord>,
    calls: usize,
    solver_decisions: u64,
    saw_unknown_trial: bool,
    /// Only meaningful to emit a candidate once the whole input was proved UNSAT.
    input_proved_unsat: bool,
    /// Set the first time a solver call itself reports that cancellation was observed.
    cancellation_observed: bool,
}

/// Run extraction using `solver`, optionally certify with `verifier`.
pub fn extract(
    request_id: &str,
    formula: &Formula,
    solver: &dyn SatSolver,
    verifier: Option<&dyn SatSolver>,
    config: &ExtractionConfig,
    cancel: Option<&AtomicBool>,
) -> Result<ExtractionReport, String> {
    formula.validate().map_err(|e| e.to_string())?;

    let redactor = Redactor::new(formula);
    let fingerprint = redactor.fingerprint().to_string();

    let mut order_positions: Vec<usize> = (0..formula.clauses.len()).collect();
    match config.order {
        OrderPolicy::Input => {}
        OrderPolicy::ShortestFirst => order_positions.sort_by_key(|&p| {
            (formula.clauses[p].literals.len(), p)
        }),
        OrderPolicy::LongestFirst => order_positions.sort_by(|&a, &b| {
            formula.clauses[b]
                .literals
                .len()
                .cmp(&formula.clauses[a].literals.len())
                .then(a.cmp(&b))
        }),
    }

    let n = formula.clauses.len();
    let mut st = RunState {
        formula,
        order_positions,
        kept: vec![true; n],
        proofs: vec![ProofState::Untested; n],
        decisions: Vec::with_capacity(n),
        calls: 0,
        solver_decisions: 0,
        saw_unknown_trial: false,
        input_proved_unsat: false,
        cancellation_observed: false,
    };

    // --- 0. Cancellation observed before any work ---
    if let Some(flag) = cancel {
        if flag.load(std::sync::atomic::Ordering::Relaxed) {
            return Ok(finalize(
                request_id,
                &mut st,
                solver,
                verifier,
                config,
                cancel,
                Outcome::Cancelled,
                StopReason::Cancelled,
                "cancellation requested before extraction started; full input returned untested",
                Some("no clause was tried; nothing was verified".to_string()),
                &fingerprint,
            ));
        }
    }

    // --- 1. Whole-input check ---
    let all = vec![true; n];
    if let Some(budget) = config.max_solver_calls {
        if st.calls >= budget {
            return Ok(finalize(
                request_id,
                &mut st,
                solver,
                verifier,
                config,
                cancel,
                Outcome::BudgetExhausted,
                StopReason::BudgetExhausted,
                format!("solver-call budget {budget} exhausted before the initial check"),
                Some("input never checked for satisfiability".to_string()),
                &fingerprint,
            ));
        }
    }
    let initial = solver.solve(formula, &all, &config.solve_limits, cancel);
    st.calls += 1;
    st.solver_decisions += initial.decisions;
    let cancel_flag_set = cancel
        .map(|f| f.load(std::sync::atomic::Ordering::Relaxed))
        .unwrap_or(false);
    if cancel_flag_set && initial.status == SolveStatus::Unknown {
        st.cancellation_observed = true;
    }

    if initial.status == SolveStatus::Sat {
        return Ok(finalize(
            request_id,
            &mut st,
            solver,
            verifier,
            config,
            cancel,
            Outcome::InputSat,
            StopReason::InputSat,
            "input formula is satisfiable; no unsatisfiable core exists",
            Some("no core requested for a satisfiable input".to_string()),
            &fingerprint,
        ));
    }
    if initial.status == SolveStatus::Unknown {
        return Ok(finalize(
            request_id,
            &mut st,
            solver,
            verifier,
            config,
            cancel,
            Outcome::InputUnknown,
            StopReason::InputUnknown,
            format!(
                "initial satisfiability check was UNKNOWN ({}) — not treated as UNSAT; no core reported",
                initial.reason.as_deref().unwrap_or("solver gave no reason")
            ),
            Some("UNKNOWN is never treated as UNSAT".to_string()),
            &fingerprint,
        ));
    }
    st.input_proved_unsat = true;

    if n == 1 {
        // Singleton contradiction: the initial UNSAT check is the whole proof.
        st.proofs[0] = ProofState::InitialCheck;
    }

    // --- 2. Per-clause deletion trials ---
    for seq in 0..st.order_positions.len() {
        let p = st.order_positions[seq];
        if !st.kept[p] {
            continue;
        }

        if let Some(flag) = cancel {
            if flag.load(std::sync::atomic::Ordering::Relaxed) {
                return Ok(finalize(
                    request_id,
                    &mut st,
                    solver,
                    verifier,
                    config,
                    cancel,
                    Outcome::Cancelled,
                    StopReason::Cancelled,
                    "cancelled during deletion trials; verified candidates and their proof states retained",
                    None,
                    &fingerprint,
                ));
            }
        }
        if let Some(budget) = config.max_solver_calls {
            if st.calls >= budget {
                return Ok(finalize(
                    request_id,
                    &mut st,
                    solver,
                    verifier,
                    config,
                    cancel,
                    Outcome::BudgetExhausted,
                    StopReason::BudgetExhausted,
                    format!(
                        "solver-call budget {budget} exhausted before trial of '{}'; remaining candidates kept untested",
                        formula.clauses[p].id
                    ),
                    None,
                    &fingerprint,
                ));
            }
        }

        st.kept[p] = false;
        let outcome = solver.solve(formula, &st.kept, &config.solve_limits, cancel);
        st.calls += 1;
        st.solver_decisions += outcome.decisions;
        if outcome.status == SolveStatus::Unknown
            && cancel
                .map(|f| f.load(std::sync::atomic::Ordering::Relaxed))
                .unwrap_or(false)
        {
            st.cancellation_observed = true;
        }

        match outcome.status {
            SolveStatus::Unsat => {
                // Contradiction survives deletion: clause is redundant in this subset.
                record_decision(
                    &mut st,
                    &redactor,
                    request_id,
                    seq,
                    p,
                    SolveStatus::Unsat,
                    DecisionAction::Removed,
                    "trial of (candidate minus clause) = UNSAT: clause redundant, permanently removed",
                    outcome.reason.clone(),
                );
            }
            SolveStatus::Sat => {
                // Contradiction needs this clause: restore and record witness.
                st.kept[p] = true;
                st.proofs[p] = ProofState::SatWitness;
                record_decision(
                    &mut st,
                    &redactor,
                    request_id,
                    seq,
                    p,
                    SolveStatus::Sat,
                    DecisionAction::Kept,
                    "trial of (candidate minus clause) = SAT: clause participates in the surviving contradiction, kept",
                    None,
                );
            }
            SolveStatus::Unknown => {
                // Conservative retention; minimality cannot be claimed for this member.
                st.kept[p] = true;
                st.proofs[p] = ProofState::UnknownTrial;
                st.saw_unknown_trial = true;
                record_decision(
                    &mut st,
                    &redactor,
                    request_id,
                    seq,
                    p,
                    SolveStatus::Unknown,
                    DecisionAction::KeptUndecided,
                    "trial of (candidate minus clause) = UNKNOWN: cannot prove participation, conservatively kept",
                    outcome.reason.clone(),
                );
            }
        }
    }

    let (outcome, reason, summary) = if st.saw_unknown_trial {
        (
            Outcome::CompletedWithUnknownTrials,
            StopReason::CompletedWithUnknownTrials,
            "all clauses tried, but at least one trial was UNKNOWN: candidate complete but subset-minimality unproven for those members",
        )
    } else {
        (
            Outcome::Completed,
            StopReason::Completed,
            "all deletion trials finished; every retained member has a SAT without-it witness",
        )
    };
    Ok(finalize(
        request_id,
        &mut st,
        solver,
        verifier,
        config,
        cancel,
        outcome,
        reason,
        summary,
        None,
        &fingerprint,
    ))
}

#[allow(clippy::too_many_arguments)]
fn finalize(
    request_id: &str,
    st: &mut RunState<'_>,
    solver: &dyn SatSolver,
    verifier: Option<&dyn SatSolver>,
    config: &ExtractionConfig,
    cancel: Option<&AtomicBool>,
    outcome: Outcome,
    stop_reason: StopReason,
    summary: impl Into<String>,
    skip_reason: Option<String>,
    fingerprint: &str,
) -> ExtractionReport {
    // Candidate in *formula* order for stable output.
    // A candidate is only emitted once the input was actually proved UNSAT: SAT and
    // UNKNOWN short-circuits (and a pre-check budget stop) return an empty core
    // instead of presenting all input clauses as one.
    let report_candidate = st.input_proved_unsat;
    let mut core: Vec<ClauseId> = Vec::new();
    let mut member_proofs: Vec<MemberProof> = Vec::new();
    let mut core_positions: Vec<usize> = Vec::new();
    if report_candidate {
        for p in 0..st.kept.len() {
            if st.kept[p] {
                core.push(st.formula.clauses[p].id.clone());
                member_proofs.push(MemberProof {
                    id: st.formula.clauses[p].id.clone(),
                    state: st.proofs[p],
                });
                core_positions.push(p);
            }
        }
    }

    // Only a fully traversed, no-unknown run is eligible for certification.
    let mut verification = None;
    let mut verification_skipped_reason = skip_reason;
    if config.verify {
        match outcome {
            Outcome::Completed => {
                if let Some(v) = verifier {
                    let report = certify(
                        st.formula,
                        &core_positions,
                        v,
                        &config.solve_limits,
                        cancel,
                    );
                    verification = Some(report);
                } else {
                    verification_skipped_reason = Some(
                        "verification requested but no verifier solver configured".to_string(),
                    );
                }
            }
            Outcome::CompletedWithUnknownTrials
            | Outcome::BudgetExhausted
            | Outcome::Cancelled => {
                verification_skipped_reason = Some(format!(
                    "independent certification requires a complete run; run stopped as {:?}",
                    outcome
                ));
            }
            Outcome::InputSat | Outcome::InputUnknown => {}
        }
    } else {
        verification_skipped_reason = Some("verification disabled by request".to_string());
    }

    let mut warnings = Vec::new();
    if member_proofs
        .iter()
        .any(|m| m.state == ProofState::UnknownTrial)
    {
        warnings.push("one or more members kept on UNKNOWN trials are not proven members".into());
    }
    if member_proofs.iter().any(|m| m.state == ProofState::Untested) {
        warnings.push("one or more candidates were never tried; candidate may be non-minimal".into());
    }

    ExtractionReport {
        request_id: request_id.to_string(),
        outcome,
        stop_reason,
        summary: summary.into(),
        core,
        member_proofs,
        verification,
        verification_skipped_reason,
        decisions: std::mem::take(&mut st.decisions),
        solver_calls: st.calls,
        solver_decisions: st.solver_decisions,
        budget_limit: config.max_solver_calls,
        input_clause_count: st.formula.clauses.len(),
        core_clause_count: core_positions.len(),
        fingerprint: fingerprint.to_string(),
        solver: solver.name().to_string(),
        warnings,
        cancellation_observed: st.cancellation_observed,
    }
}

#[allow(clippy::too_many_arguments)]
fn record_decision(
    st: &mut RunState<'_>,
    redactor: &Redactor,
    request_id: &str,
    seq: usize,
    position: usize,
    trial: SolveStatus,
    action: DecisionAction,
    basis: &str,
    solver_reason: Option<String>,
) {
    st.decisions.push(DecisionRecord {
        seq,
        request_id: request_id.to_string(),
        candidate: redactor.clause_ref(st.formula, position),
        trial_status: trial,
        action,
        basis: basis.to_string(),
        solver_calls_used: st.calls,
        decisions_spent: st.solver_decisions,
        solver_reason,
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::language::{Clause, Literal};
    use crate::solver::dpll::DpllSolver;
    use crate::solver::{SolveLimits, SolveOutcome, SatSolver};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    fn lit(n: i64) -> Literal {
        Literal::from_dimacs(n).unwrap()
    }

    fn clause(id: &str, lits: &[i64]) -> Clause {
        Clause {
            id: ClauseId::new(id),
            literals: lits.iter().map(|n| lit(*n)).collect(),
            sensitive: false,
        }
    }

    fn basic_formula() -> Formula {
        // Two overlapping MUS: {a,b,c} and {c,d,e}, plus redundant clause 'g'.
        Formula {
            clauses: vec![
                clause("a", &[1]),
                clause("b", &[2]),
                clause("c", &[-1, -2]),
                clause("d", &[3]),
                clause("e", &[-2, -3]),
                clause("g", &[4]),
            ],
        }
    }

    fn cfg(budget: Option<usize>) -> ExtractionConfig {
        ExtractionConfig {
            max_solver_calls: budget,
            verify: false,
            ..Default::default()
        }
    }

    #[test]
    fn deletes_redundant_clause_and_certifies_set_minimality() {
        let f = basic_formula();
        let s = DpllSolver::new();
        let report = extract("t1", &f, &s, None, &cfg(None), None).unwrap();
        assert_eq!(report.outcome, Outcome::Completed);
        // 'g' (clause 4) is redundant: it must be removed.
        assert!(!report.core.contains(&ClauseId::new("g")));
        // Every kept member has the strong proof.
        assert!(report
            .member_proofs
            .iter()
            .all(|m| matches!(m.state, ProofState::SatWitness | ProofState::InitialCheck)));
        assert_eq!(report.core_clause_count, 3);
    }

    #[test]
    fn budget_exhaustion_is_a_distinct_outcome() {
        let f = basic_formula();
        let s = DpllSolver::new();
        // Initial check is call 1; only one trial is affordable.
        let report = extract("t2", &f, &s, None, &cfg(Some(2)), None).unwrap();
        assert_eq!(report.outcome, Outcome::BudgetExhausted);
        assert_eq!(report.solver_calls, 2);
        assert!(report
            .member_proofs
            .iter()
            .any(|m| m.state == ProofState::Untested));
    }

    #[test]
    fn unknown_never_means_unsat() {
        // Solver that says UNKNOWN for everything: the input check must short-circuit.
        struct Unknowner;
        impl SatSolver for Unknowner {
            fn name(&self) -> &str {
                "unknowner"
            }
            fn solve(
                &self,
                _f: &Formula,
                _m: &[bool],
                _l: &SolveLimits,
                _c: Option<&AtomicBool>,
            ) -> SolveOutcome {
                SolveOutcome::unknown("stub always unknown", 0)
            }
        }
        let f = basic_formula();
        let report = extract("t3", &f, &Unknowner, None, &cfg(None), None).unwrap();
        assert_eq!(report.outcome, Outcome::InputUnknown);
        assert!(report.core.is_empty());
    }

    #[test]
    fn cancellation_preserves_verified_candidates() {
        // Solver that allows the initial UNSAT check, then observes cancellation.
        struct CancelAware {
            flag: Arc<AtomicBool>,
            inner: DpllSolver,
        }
        impl SatSolver for CancelAware {
            fn name(&self) -> &str {
                "cancel-aware"
            }
            fn solve(
                &self,
                f: &Formula,
                m: &[bool],
                l: &SolveLimits,
                _c: Option<&AtomicBool>,
            ) -> SolveOutcome {
                let first_call = !self.flag.load(Ordering::Relaxed);
                let out = self.inner.solve(f, m, l, None);
                if first_call {
                    self.flag.store(true, Ordering::Relaxed);
                }
                out
            }
        }
        let flag = Arc::new(AtomicBool::new(false));
        let solver = CancelAware {
            flag: flag.clone(),
            inner: DpllSolver::new(),
        };
        let f = basic_formula();
        let report = extract("t4", &f, &solver, None, &cfg(None), Some(&flag)).unwrap();
        assert_eq!(report.outcome, Outcome::Cancelled);
        // Nothing was deleted on the cancelled path...
        assert!(report.decisions.iter().all(|d| d.action != DecisionAction::Removed));
        // ...and everything retained is explicitly marked untested, not "proven".
        assert!(report
            .member_proofs
            .iter()
            .all(|m| m.state == ProofState::Untested));
    }

    #[test]
    fn satisfiable_input_has_no_core() {
        let f = Formula {
            clauses: vec![clause("a", &[1]), clause("b", &[2])],
        };
        let s = DpllSolver::new();
        let report = extract("t5", &f, &s, None, &cfg(None), None).unwrap();
        assert_eq!(report.outcome, Outcome::InputSat);
        assert!(report.core.is_empty());
    }

    #[test]
    fn empty_clause_is_singleton_core() {
        let f = Formula {
            clauses: vec![clause("bang", &[])],
        };
        let s = DpllSolver::new();
        let report = extract("t6", &f, &s, None, &cfg(None), None).unwrap();
        assert_eq!(report.outcome, Outcome::Completed);
        assert_eq!(report.core, vec![ClauseId::new("bang")]);
        // The deletion trial (empty formula is trivially SAT) supplies the stronger
        // SatWitness proof; InitialCheck would also be acceptable for n==1.
        assert!(matches!(
            report.member_proofs[0].state,
            ProofState::SatWitness | ProofState::InitialCheck
        ));
    }
}
