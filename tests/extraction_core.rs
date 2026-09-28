//! Integration tests for the extraction core.
//!
//! Every assertion here is about a concrete result or a concrete failure category.
//! Expected cores are hand-derived constants in `common::fixtures`; SAT/UNSAT and
//! subset-minimality are additionally re-proved by the independent oracle in
//! `common::oracle`, which shares no code with the implementation under test.

mod common;

use std::collections::HashSet;
use std::sync::atomic::AtomicBool;
use std::sync::Arc;

use common::fixtures::{
    clause_ints, core_a_ints, core_b_ints, disjoint_cores, disjoint_cores_reversed,
    empty_clause, overlapping_with_redundancy, ALWAYS_DELETED, OVERLAP_EXPECTED_CORE,
};
use common::oracle::{assert_reference_mus, is_sat, is_unsat, model_satisfies};
use common::{
    assert_all_members_witnessed, assert_core_ids, clause, test_cfg, CancelAfterSolver,
    ScriptedSolver,
};

use mus_service::core::{ExtractionConfig, Outcome, ProofState};
use mus_service::evidence::Verdict;
use mus_service::language::Formula;
use mus_service::solver::brute::BruteForceSolver;
use mus_service::solver::dpll::DpllSolver;
use mus_service::solver::{SatSolver, SolveLimits, SolveOutcome, SolveStatus};

fn ids_to_ints(f: &Formula, ids: &[&str]) -> Vec<Vec<i64>> {
    ids.iter().map(|id| clause_ints(f, id)).collect()
}

// ---------------------------------------------------------------------------
// 1. Overlapping cores + redundancy
// ---------------------------------------------------------------------------

#[test]
fn overlapping_conflicts_with_redundancy_returns_expected_core() {
    let f = overlapping_with_redundancy();

    // Reference answers derived independently of the service:
    assert_reference_mus(&core_a_ints());
    assert_reference_mus(&core_b_ints());

    let solver = DpllSolver::new();
    let verifier = BruteForceSolver::new(24);
    let mut cfg = test_cfg();
    cfg.verify = true;

    let report = common::extract("req-overlap", &f, &solver, Some(&verifier), &cfg, None)
        .expect("valid input");

    assert_eq!(report.outcome, Outcome::Completed);
    assert_core_ids(&report, OVERLAP_EXPECTED_CORE);
    assert_all_members_witnessed(&report);

    // Independent oracle confirms the reported core satisfies the definition directly.
    let reported = ids_to_ints(&f, OVERLAP_EXPECTED_CORE);
    assert_reference_mus(&reported);

    // All redundancy classes that must never survive ANY core were actually deleted.
    let core_set: HashSet<&str> = report.core.iter().map(|i| i.0.as_str()).collect();
    for redundant in ALWAYS_DELETED {
        assert!(!core_set.contains(redundant), "{redundant} must be removed");
    }
    // r_dup_c has the same CONTENT as c but a distinct identity: it is tracked as a
    // separate constraint and is NOT silently merged/deduplicated away here.
    assert!(core_set.contains("r_dup_c"));

    // Every deletion decision in the audit trail must cite an UNSAT trial; every
    // retention must cite a SAT trial (records are the "basis" of each judgement).
    use mus_service::diag::DecisionAction;
    for d in &report.decisions {
        match d.action {
            DecisionAction::Removed => assert_eq!(d.trial_status, SolveStatus::Unsat),
            DecisionAction::Kept => assert_eq!(d.trial_status, SolveStatus::Sat),
            DecisionAction::KeptUndecided => panic!("no UNKNOWN trials expected here"),
        }
        assert!(!d.basis.is_empty(), "each decision must carry a basis");
        assert_eq!(d.request_id, "req-overlap");
    }

    // In-process independent verifier must agree.
    let v = report.verification.expect("verification ran");
    assert_eq!(v.verdict, Verdict::Certified);
    // It made exactly 1 + |core| calls and each SAT witness really satisfies core-i.
    assert_eq!(v.verifier_calls, 1 + report.core.len());
    let core_clause_ints: Vec<Vec<i64>> =
        report.core.iter().map(|id| clause_ints(&f, &id.0)).collect();
    for ev in &v.member_evidence {
        assert_eq!(ev.status, SolveStatus::Sat);
        let model = ev.witness_model.clone().expect("SAT trial has a model");
        let mut reduced = core_clause_ints.clone();
        let pos = report
            .core
            .iter()
            .position(|id| id == &ev.member)
            .unwrap();
        reduced.remove(pos);
        assert!(
            model_satisfies(&reduced, &model),
            "witness for deleting {} does not satisfy the reduced core",
            ev.member.0
        );
    }
}

// ---------------------------------------------------------------------------
// 2. Subset-minimal is not cardinality-minimal
// ---------------------------------------------------------------------------

#[test]
fn subset_minimality_is_not_cardinality_minimality() {
    let solver = DpllSolver::new();
    let cfg = test_cfg();

    let f_ab = disjoint_cores();
    let report_ab = common::extract("req-ab", &f_ab, &solver, None, &cfg, None).unwrap();
    assert_eq!(report_ab.outcome, Outcome::Completed);
    // Input order lists core A (size 3) first; deleting a/b/c leaves core B alive,
    // so deletion returns the size-2 core B.
    assert_core_ids(&report_ab, &["d", "e"]);
    assert_eq!(report_ab.core_clause_count, 2);
    assert_reference_mus(&ids_to_ints(&f_ab, &["d", "e"]));

    // Same clauses, core B listed first: now A survives, returning the size-3 core.
    let f_ba = disjoint_cores_reversed();
    let report_ba = common::extract("req-ba", &f_ba, &solver, None, &cfg, None).unwrap();
    assert_core_ids(&report_ba, &["a", "b", "c"]);
    assert_eq!(report_ba.core_clause_count, 3);
    assert_reference_mus(&ids_to_ints(&f_ba, &["a", "b", "c"]));

    // The two valid answers have different cardinality (2 vs 3); both are valid MUS
    // and the service never claims it returned the minimum-cardinality one.
    assert_ne!(report_ab.core_clause_count, report_ba.core_clause_count);
    assert!(report_ab.core_clause_count < report_ba.core_clause_count);
}

// ---------------------------------------------------------------------------
// 3. Budget exhaustion — a distinct, audited failure category
// ---------------------------------------------------------------------------

#[test]
fn exhausted_call_budget_retains_untested_candidates_with_weak_state() {
    let f = overlapping_with_redundancy();
    let solver = DpllSolver::new();
    let cfg = ExtractionConfig {
        max_solver_calls: Some(4), // initial + 3 trials
        // Verification enabled on purpose: an incomplete run must still be denied a
        // certificate with an explicit reason (not silently conflated with "disabled").
        verify: true,
        ..test_cfg()
    };
    let report = common::extract("req-budget", &f, &solver, None, &cfg, None).unwrap();

    assert_eq!(
        report.outcome,
        Outcome::BudgetExhausted,
        "budget overflow must be its own category, not completion or UNKNOWN"
    );
    assert_eq!(report.solver_calls, 4);
    assert_eq!(report.budget_limit, Some(4));
    assert!(report
        .member_proofs
        .iter()
        .any(|m| m.state == ProofState::Untested));
    assert!(report
        .warnings
        .iter()
        .any(|w| w.contains("never tried")));
    // No certification on an incomplete run — and the report says why.
    assert!(report.verification.is_none());
    assert!(report
        .verification_skipped_reason
        .as_deref()
        .unwrap_or_default()
        .contains("complete run"));
}

// ---------------------------------------------------------------------------
// 4. UNKNOWN must never be treated as UNSAT
// ---------------------------------------------------------------------------

#[test]
fn unknown_on_initial_check_is_input_unknown_not_unsat() {
    let f = overlapping_with_redundancy();
    let solver = ScriptedSolver::new("always-unknown", |_n, _mask| {
        SolveOutcome::unknown("simulated solver timeout", 0)
    });
    let report = common::extract("req-unk1", &f, &solver, None, &test_cfg(), None).unwrap();
    assert_eq!(report.outcome, Outcome::InputUnknown);
    assert!(report.core.is_empty(), "no core may be reported from UNKNOWN");
    assert!(report.summary.contains("UNKNOWN"));
}

#[test]
fn unknown_mid_run_conservatively_keeps_and_downgrades_outcome() {
    // Only the single ambiguous judgement is scripted (the first deletion trial);
    // the genuine initial UNSAT check and every later trial run on the real DPLL.
    let f = overlapping_with_redundancy();
    let f_for_closure = f.clone();
    let real = DpllSolver::new();
    let solver = ScriptedSolver::new("one-unknown", move |n, mask| {
        if n == 1 {
            SolveOutcome::unknown("simulated timeout on first trial", 0)
        } else {
            real.solve(&f_for_closure, mask, &SolveLimits::default(), None)
        }
    });
    let report = common::extract("req-unk2", &f, &solver, None, &test_cfg(), None).unwrap();

    assert_eq!(report.outcome, Outcome::CompletedWithUnknownTrials);
    // First trial in input order is 'a': ambiguous -> conservatively kept, weak state.
    let a = report
        .member_proofs
        .iter()
        .find(|m| m.id.0 == "a")
        .expect("a retained");
    assert_eq!(a.state, ProofState::UnknownTrial);
    let decision = report
        .decisions
        .iter()
        .find(|d| d.candidate.id.0 == "a")
        .unwrap();
    assert_eq!(decision.trial_status, SolveStatus::Unknown);
    assert!(report
        .warnings
        .iter()
        .any(|w| w.contains("UNKNOWN")));
}

// ---------------------------------------------------------------------------
// 5. Cancellation preserves already verified candidates + proof states
// ---------------------------------------------------------------------------

#[test]
fn cancellation_keeps_candidates_and_marks_untested_honestly() {
    let f = overlapping_with_redundancy();
    let flag = Arc::new(AtomicBool::new(false));
    // Initial UNSAT check completes, zero trials complete: the run stops at the first
    // deletion-phase boundary it observes.
    let solver = CancelAfterSolver::new(flag.clone(), 0);
    let report = common::extract("req-cancel", &f, &solver, None, &test_cfg(), Some(&flag))
        .unwrap();

    assert_eq!(report.outcome, Outcome::Cancelled);
    // No clause was retained on a SAT trial on this early-cancel path: every clause
    // still in the candidate is explicitly Untested, never falsely presented as proven.
    assert!(report
        .member_proofs
        .iter()
        .all(|m| m.state == ProofState::Untested));
    // The already-taken deletion decisions (if any finished before the boundary) are
    // recorded with their exact basis rather than rolled back silently.
    for d in &report.decisions {
        assert!(!d.basis.is_empty());
        assert_eq!(d.request_id, "req-cancel");
    }
}

#[test]
fn cancellation_after_some_trials_keeps_proven_state_distinct_from_untested() {
    let f = overlapping_with_redundancy();
    let flag = Arc::new(AtomicBool::new(false));
    // Let the initial check and the first four deletion trials finish. Trials 1..3
    // (a,b,c) delete via UNSAT; trial 4 (d) KEEPS via SAT, so at least one strong
    // witness exists when the boundary is next reached.
    let solver = CancelAfterSolver::new(flag.clone(), 4);
    let report = common::extract("req-cancel2", &f, &solver, None, &test_cfg(), Some(&flag))
        .unwrap();

    assert_eq!(report.outcome, Outcome::Cancelled);
    let states: Vec<ProofState> = report.member_proofs.iter().map(|m| m.state).collect();
    // At least one clause was already decided strongly (SAT without-it witness)...
    assert!(
        states.contains(&ProofState::SatWitness),
        "expected a proven member, got {states:?}"
    );
    // ...and at least one later clause honestly marked untested.
    assert!(
        states.contains(&ProofState::Untested),
        "expected an untested member, got {states:?}"
    );
}

// ---------------------------------------------------------------------------
// 6. Special inputs: SAT input, empty clause, identity contract
// ---------------------------------------------------------------------------

#[test]
fn satisfiable_input_is_rejected_as_a_category() {
    let f = Formula {
        clauses: vec![clause("x", &[1]), clause("y", &[2])],
    };
    let report = common::extract("req-sat", &f, &DpllSolver::new(), None, &test_cfg(), None)
        .unwrap();
    assert_eq!(report.outcome, Outcome::InputSat);
    assert!(report.core.is_empty());
}

#[test]
fn empty_clause_is_a_certified_singleton_core() {
    let f = empty_clause();
    let verifier = BruteForceSolver::default();
    let mut cfg = test_cfg();
    cfg.verify = true;
    let report = common::extract("req-empty", &f, &DpllSolver::new(), Some(&verifier), &cfg, None)
        .unwrap();
    assert_eq!(report.outcome, Outcome::Completed);
    assert_core_ids(&report, &["bang"]);
    assert!(matches!(
        report.member_proofs[0].state,
        ProofState::SatWitness | ProofState::InitialCheck
    ));
    assert_eq!(
        report.verification.unwrap().verdict,
        Verdict::Certified
    );
}

#[test]
fn duplicate_identities_are_a_validation_error_not_a_solver_problem() {
    let f = Formula {
        clauses: vec![clause("same", &[1]), clause("same", &[-1])],
    };
    let err = common::extract(
        "req-dup",
        &f,
        &DpllSolver::new(),
        None,
        &test_cfg(),
        None,
    )
    .expect_err("duplicate ids rejected");
    assert!(err.contains("duplicate clause id"), "got: {err}");
}

// ---------------------------------------------------------------------------
// 7. Sanity: the independent oracle genuinely disagrees with satisfiable junk
// ---------------------------------------------------------------------------

#[test]
fn oracle_itself_distinguishes_the_fixtures() {
    assert!(is_unsat(&core_a_ints()));
    assert!(is_unsat(&core_b_ints()));
    // core A minus any one member is SAT — checked exhaustively inside assert_reference_mus.
    let mut minus_b = core_a_ints();
    minus_b.remove(1);
    assert!(is_sat(&minus_b));
}
