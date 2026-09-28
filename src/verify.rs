//! Independent evidence verification.
//!
//! The verifier deliberately uses a *different* [`Solver`] instance/backend than the
//! one that produced a report — the whole point is that the extractor cannot vouch
//! for itself. It checks:
//!
//! 1. Every emitted core is UNSAT as a whole.
//! 2. For every member m, `core \\ {m}` is SAT **and** the recorded witness truly
//!    satisfies that set and falsifies nothing (witnesses are re-evaluated here, not
//!    trusted as opaque blobs).
//! 3. [`audit_trace`] re-runs every deletion decision against the recorded id-sets
//!    with the independent backend and flags any mismatch — so a lying/buggy kernel
//!    cannot get a certified verdict past the API boundary.
//!
//! Scope/trade-off (see README): UNSAT is cross-checked with an independent SAT
//! solver rather than with a machine-checked resolution/refutation certificate.
//! A genuine DRAT/FRAT proof checker is the natural next plug-in behind the same
//! interface.

use crate::extract::{ExtractionReport, FoundCore, Phase, TraceEntry};
use crate::language::Cnf;
use crate::solver::{SStatus, SolveCtx, Solver, Budget, CancelToken};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CoreCheck {
    /// UNSAT as a whole and SAT upon removing each member (witness verified).
    CertifiedMus,
    /// Core is SAT — it cannot be an unsat core at all.
    NotUnsat,
    /// Some member can be removed while remaining UNSAT — not subset-minimal.
    NotMinimal,
    /// Witness exists in the report but fails to satisfy the claimed formula.
    BadWitness,
    /// Independent backend could not decide a required query.
    Inconclusive,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CoreVerification {
    pub index: usize,
    pub member_ids: Vec<String>,
    pub result: CoreCheck,
    /// Member-level detail: which removal failed and why.
    pub member_failures: BTreeMap<String, String>,
    pub independent_solver: String,
    pub solver_calls: u64,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct VerificationReport {
    pub cores: Vec<CoreVerification>,
    /// Per-trace-entry `ok` flags (true = independent verdict matches the log).
    pub trace_audit: Vec<TraceAuditEntry>,
    pub all_certified: bool,
    pub independent_solver: String,
    pub solver_calls: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TraceAuditEntry {
    pub seq: u64,
    pub phase: Phase,
    pub ok: bool,
    pub expected: String,
    pub independent: String,
    pub reason: Option<String>,
}

/// Verify one core with an independent backend.
#[must_use]
pub fn verify_core(
    cnf: &Cnf,
    core: &FoundCore,
    oracle: &dyn Solver,
    budget_limit: u64,
) -> CoreVerification {
    verify_core_with(cnf, core, oracle, budget_limit, &CancelToken::default())
}

#[must_use]
fn verify_core_with(
    cnf: &Cnf,
    core: &FoundCore,
    oracle: &dyn Solver,
    budget_limit: u64,
    cancel: &CancelToken,
) -> CoreVerification {
    let budget = Budget::new(budget_limit);
    let ctx = SolveCtx::new(budget.clone(), cancel.clone());
    let mut calls = 0u64;
    let mut member_failures = BTreeMap::new();
    let member_set: BTreeSet<String> = core.member_ids.iter().cloned().collect();

    // 1. Whole core must be UNSAT.
    let whole = oracle.solve(&cnf.subset(&member_set), &ctx);
    calls += 1;
    let result = match whole.status {
        SStatus::Sat => CoreCheck::NotUnsat,
        SStatus::Unknown => CoreCheck::Inconclusive,
        SStatus::Unsat => {
            let mut verdict = CoreCheck::CertifiedMus;
            for m in &core.member_ids {
                let mut trial = member_set.clone();
                trial.remove(m);
                let sub = cnf.subset(&trial);
                let r = oracle.solve(&sub, &ctx);
                calls += 1;
                match r.status {
                    SStatus::Unsat => {
                        verdict = CoreCheck::NotMinimal;
                        member_failures
                            .insert(m.clone(), format!("core \\ {{{m}}} is still UNSAT"));
                    }
                    SStatus::Unknown => {
                        verdict = CoreCheck::Inconclusive;
                        member_failures.insert(
                            m.clone(),
                            r.detail.unwrap_or_else(|| "independent solver unknown".to_string()),
                        );
                    }
                    SStatus::Sat => {
                        // Prefer the witness just produced; if the report carries one,
                        // validate that one independently instead of trusting it.
                        if let Some(recorded) = core.minimality_witnesses.get(m) {
                            if sub.satisfied_by(recorded) {
                                // good
                            } else {
                                verdict = CoreCheck::BadWitness;
                                member_failures.insert(
                                    m.clone(),
                                    "recorded witness does not satisfy core \\ {m}".to_string(),
                                );
                            }
                        } else if let Some(got) = &r.model {
                            if !sub.satisfied_by(got) {
                                verdict = CoreCheck::BadWitness;
                                member_failures
                                    .insert(m.clone(), "oracle witness failed self-check".to_string());
                            }
                        } else {
                            verdict = CoreCheck::Inconclusive;
                            member_failures
                                .insert(m.clone(), "SAT reported without a witness".to_string());
                        }
                    }
                }
            }
            verdict
        }
    };

    CoreVerification {
        index: 0,
        member_ids: core.member_ids.clone(),
        result,
        member_failures,
        independent_solver: oracle.name().to_string(),
        solver_calls: calls,
    }
}

/// Verify all cores in a report and audit the full deletion trace.
#[must_use]
pub fn verify_report(
    cnf: &Cnf,
    report: &ExtractionReport,
    oracle: &dyn Solver,
    per_core_budget: u64,
    trace_budget: u64,
) -> VerificationReport {
    let cancel = CancelToken::default();
    let mut total_calls = 0u64;
    let mut all_certified = !report.cores.is_empty();

    let mut core_results = Vec::new();
    for (i, core) in report.cores.iter().enumerate() {
        let mut v = verify_core_with(cnf, core, oracle, per_core_budget, &cancel);
        v.index = i;
        total_calls += v.solver_calls;
        if v.result != CoreCheck::CertifiedMus {
            all_certified = false;
        }
        core_results.push(v);
    }

    let trace_audit = audit_trace(cnf, report, oracle, trace_budget, &cancel, &mut total_calls);

    VerificationReport {
        cores: core_results,
        trace_audit,
        all_certified,
        independent_solver: oracle.name().to_string(),
        solver_calls: total_calls,
    }
}

/// Re-run every recorded solver decision on exactly the logged id-set and compare.
#[must_use]
fn audit_trace(
    cnf: &Cnf,
    report: &ExtractionReport,
    oracle: &dyn Solver,
    budget_limit: u64,
    cancel: &CancelToken,
    total_calls: &mut u64,
) -> Vec<TraceAuditEntry> {
    let budget = Budget::new(budget_limit);
    let ctx = SolveCtx::new(budget, cancel.clone());

    let mut out = Vec::new();
    for e in &report.trace {
        let trial: BTreeSet<String> = e.trial_ids.iter().cloned().collect();
        let r = oracle.solve(&cnf.subset(&trial), &ctx);
        *total_calls += 1;
        let got = match r.status {
            SStatus::Sat => "sat",
            SStatus::Unsat => "unsat",
            SStatus::Unknown => "unknown",
        };
        let ok = got == e.verdict;
        out.push(TraceAuditEntry {
            seq: e.seq,
            phase: e.phase,
            ok,
            expected: e.verdict.clone(),
            independent: got.to_string(),
            reason: if ok {
                None
            } else {
                Some(format!(
                    "logged {:?} but independent backend says {got}",
                    e.verdict
                ))
            },
        });
    }
    out
}

/// Convenience for tests: audit entries only (typed return).
pub fn audit_entries(
    cnf: &Cnf,
    entries: &[TraceEntry],
    oracle: &dyn Solver,
) -> Vec<(u64, bool)> {
    let report = ExtractionReport {
        termination: crate::extract::Termination::Completed,
        input_satisfiable: false,
        cores: Vec::new(),
        retained_candidate: Vec::new(),
        untested: Vec::new(),
        trace: entries.to_vec(),
        solver: String::new(),
        budget_limit: 0,
        budget_used: 0,
        rounds: 1,
        note: None,
    };
    let mut calls = 0u64;
    audit_trace(cnf, &report, oracle, 0, &CancelToken::default(), &mut calls)
        .into_iter()
        .map(|a| (a.seq, a.ok))
        .collect()
}
