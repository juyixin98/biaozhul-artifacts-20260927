//! Tests for the evidence layer, including the case where the independent verifier
//! itself cannot decide: certification must degrade to Inconclusive, never acceptance.

mod common;

use std::sync::atomic::AtomicBool;
use std::sync::Arc;

use common::fixtures::{clause_ints, overlapping_with_redundancy, OVERLAP_EXPECTED_CORE};
use common::oracle::assert_reference_mus;
use common::{test_cfg};

use mus_service::evidence::{certify, Verdict};
use mus_service::language::{ClauseId, Formula};
use mus_service::solver::brute::BruteForceSolver;
use mus_service::solver::{SolveLimits, SolveOutcome, SolveStatus, SatSolver};

fn positions_of(f: &Formula, ids: &[&str]) -> Vec<usize> {
    ids.iter()
        .map(|id| {
            f.clauses
                .iter()
                .position(|c| c.id == ClauseId::new(*id))
                .unwrap_or_else(|| panic!("missing {id}"))
        })
        .collect()
}

#[test]
fn verifier_certifies_a_reference_minimal_core() {
    let f = overlapping_with_redundancy();
    let positions = positions_of(&f, OVERLAP_EXPECTED_CORE);
    let verifier = BruteForceSolver::new(24);

    let report = certify(
        &f,
        &positions,
        &verifier,
        &SolveLimits::default(),
        None,
    );

    assert_eq!(report.verdict, Verdict::Certified);
    assert_eq!(report.whole_status, SolveStatus::Unsat);
    assert_eq!(report.verifier_calls, 1 + OVERLAP_EXPECTED_CORE.len());
    for ev in &report.member_evidence {
        assert_eq!(ev.status, SolveStatus::Sat);
        assert!(ev.witness_model.is_some());
    }

    // Independent third implementation checks the same core from scratch.
    let ints: Vec<Vec<i64>> = OVERLAP_EXPECTED_CORE
        .iter()
        .map(|id| clause_ints(&f, id))
        .collect();
    assert_reference_mus(&ints);
}

#[test]
fn verifier_rejects_a_non_minimal_superset() {
    let f = overlapping_with_redundancy();
    // Claim the valid core plus the unrelated unit: verifier must catch redundancy.
    let positions = positions_of(&f, &["a", "d", "e", "r_unit"]);
    let verifier = BruteForceSolver::new(24);
    let report = certify(&f, &positions, &verifier, &SolveLimits::default(), None);
    assert_eq!(report.verdict, Verdict::NotMinimal);
}

#[test]
fn verifier_unknown_is_inconclusive_not_certified() {
    // Wrap the real brute solver but make ALL member-removal checks return UNKNOWN,
    // simulating an external verifier timeout.
    struct PartialVerifier {
        inner: BruteForceSolver,
    }
    impl SatSolver for PartialVerifier {
        fn name(&self) -> &str {
            "partial-verifier"
        }
        fn solve(
            &self,
            formula: &Formula,
            mask: &[bool],
            limits: &SolveLimits,
            cancel: Option<&AtomicBool>,
        ) -> SolveOutcome {
            let active = mask.iter().filter(|b| **b).count();
            let total = formula.clauses.len();
            let out = self.inner.solve(formula, mask, limits, cancel);
            if active < total && out.status == SolveStatus::Sat {
                // Pretend the verifier could not finish the member-deletion checks.
                SolveOutcome::unknown("verifier timeout on reduced subset", out.decisions)
            } else {
                out
            }
        }
    }

    let f = overlapping_with_redundancy();
    let positions = positions_of(&f, OVERLAP_EXPECTED_CORE);
    let verifier = PartialVerifier {
        inner: BruteForceSolver::new(24),
    };
    let report = certify(&f, &positions, &verifier, &SolveLimits::default(), None);
    assert_eq!(
        report.verdict,
        Verdict::Inconclusive,
        "UNKNOWN verifier trials must not certify"
    );
    assert!(report.note.unwrap().contains("neither accepted nor rejected"));
}

#[test]
fn verifier_disagreement_on_whole_core_is_rejection() {
    // A satisfiable subset presented as a core: whole-check says SAT -> CoreNotUnsat.
    let f = Formula {
        clauses: vec![
            common::clause("x", &[1]),
            common::clause("y", &[2]),
        ],
    };
    let report = certify(
        &f,
        &[0, 1],
        &BruteForceSolver::default(),
        &SolveLimits::default(),
        None,
    );
    assert_eq!(report.verdict, Verdict::CoreNotUnsat);
}

#[test]
fn end_to_end_report_carries_the_independent_certificate() {
    // Run the real pipeline and confirm the attached certificate is the verifier's,
    // not a self-reference from the extraction solver.
    let f = overlapping_with_redundancy();
    let solver = mus_service::solver::dpll::DpllSolver::new();
    let verifier = BruteForceSolver::new(24);
    let mut cfg = test_cfg();
    cfg.verify = true;
    let report =
        common::extract("req-cert", &f, &solver, Some(&verifier), &cfg, None).unwrap();
    let v = report.verification.expect("report carries a certificate");
    assert_eq!(v.verdict, Verdict::Certified);
    assert_eq!(v.verifier_solver, "brute");
    assert_ne!(v.verifier_solver, report.solver);
    let _ = Arc::new(AtomicBool::new(false)); // keep import used across toolchain editions
}
