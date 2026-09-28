//! Service-layer tests exercising the store→graph→solver→evidence pipeline
//! without HTTP, plus cross-checks against the brute-force oracle.

mod common;

use std::collections::BTreeMap;

use diffconstraints::testlog::RunLog;
use diffconstraints::{BatchOp, ConstraintService, ErrorKind};

use common::oracle::{brute_feasible, violation_ids};

fn c(id: &str, x: &str, y: &str, w: i64) -> diffconstraints::Constraint {
    diffconstraints::Constraint::new(id, x, y, w).unwrap()
}

#[test]
fn text_document_is_added_atomically() {
    let mut log = RunLog::start("service_text_atomic");
    let svc = ConstraintService::new();
    let doc = "c1: a - b <= 1\nc2: b - c <= 2\nc3: c-a<=2\n";
    let res = svc.add_text(doc);
    assert!(res.is_ok());
    assert_eq!(svc.list().len(), 3);

    // A later document with a bad line must add nothing at all.
    let bad = "good1: a - b <= 0\nbadline: 9x - y <= 1\n";
    let err = svc.add_text(bad).unwrap_err();
    assert_eq!(err.kind, ErrorKind::Input);
    assert_eq!(svc.list().len(), 3, "parse failure adds nothing");
    log.state("constraint count after failed text", svc.list().len());
    log.expected_error("input", "parse error aborts the whole text batch");
}

#[test]
fn solve_subset_by_ids_matches_oracle() {
    let mut log = RunLog::start("service_subset_solve");
    let svc = ConstraintService::new();
    let all = [
        c("good", "a", "b", 1),
        c("n1", "b", "c", -1),
        c("n2", "c", "a", -1), // good + n1 + n2: 1-1-1 = -1 cycle
    ];
    svc.batch(all.iter().cloned().map(BatchOp::Add).collect()).unwrap();

    // Subset excluding one negative-cycle edge is feasible.
    let ans = svc.solve(Some(&["good".to_string(), "n1".to_string()])).unwrap();
    match &ans.outcome {
        diffconstraints::solver::SolveOutcome::Feasible(w) => {
            let map: BTreeMap<String, i64> = w.assignment.iter().cloned().collect();
            log.state("subset witness", &map);
            // brute oracle on exactly the subset
            let sub = vec![c("good", "a", "b", 1), c("n1", "b", "c", -1)];
            assert!(brute_feasible(&sub, 1).is_some());
            assert!(violation_ids(&sub, &map).is_empty());
        }
        other => panic!("subset should be feasible: {other:?}"),
    }

    // Full set infeasible.
    let full = svc.solve(None).unwrap();
    assert!(matches!(full.outcome, diffconstraints::solver::SolveOutcome::Infeasible(_)));

    // Unknown subset id -> state conflict.
    let err = svc.solve(Some(&["ghost".to_string()])).unwrap_err();
    assert_eq!(err.kind, ErrorKind::StateConflict);
    log.pass("subset solve independent of the rest; unknown ids rejected");
}

#[test]
fn trace_contains_replayable_intermediate_states() {
    let mut log = RunLog::start("service_trace_content");
    let svc = ConstraintService::new();
    svc.batch(vec![
        BatchOp::Add(c("a", "x", "y", 2)),
        BatchOp::Add(c("b", "y", "z", 3)),
        BatchOp::Add(c("c", "z", "x", -10)), // cycle 2+3-10 = -5
    ])
    .unwrap();

    let ans = svc.solve(None).unwrap();
    match ans.outcome {
        diffconstraints::solver::SolveOutcome::Infeasible(cyc) => {
            assert_eq!(cyc.weight, -5);
            // Small instance -> detailed per-relaxation snapshots.
            assert!(!cyc.trace.passes.is_empty());
            for p in &cyc.trace.passes {
                assert!(p.relaxations > 0, "infeasible: every pass relaxes");
                let detail = p.detail.as_ref().expect("details retained for n<=64");
                assert_eq!(detail.distances.len(), 3);
            }
            assert!(cyc.trace.rationale.contains("negative"));
            log.state("final distances", cyc.trace.final_distances);
            log.state("relaxation counts per pass",
                cyc.trace.passes.iter().map(|p| p.relaxations).collect::<Vec<_>>());
            log.reasoning("infeasible", &cyc.trace.rationale);
            log.pass("trace records per-pass relaxations, distances and rationale");
        }
        _ => panic!("expected infeasible"),
    }
}

#[test]
fn revision_advances_only_on_committed_batches() {
    let s1 = ConstraintService::new();
    assert_eq!(s1.revision(), 0);
    s1.add(c("a", "x", "y", 1)).unwrap();
    assert_eq!(s1.revision(), 1);
    let err = s1.add(c("a", "z", "w", 1)).unwrap_err();
    assert_eq!(err.kind, ErrorKind::StateConflict);
    assert_eq!(s1.revision(), 1, "failed op must not bump revision");
}

#[test]
fn verify_assignment_flags_extra_and_missing_variables() {
    let svc = ConstraintService::new();
    svc.add(c("a", "x", "y", 1)).unwrap();

    let mut with_extra = BTreeMap::new();
    with_extra.insert("x".to_string(), 0i64);
    with_extra.insert("y".to_string(), 0i64);
    with_extra.insert("ghost".to_string(), 0i64);
    let check = svc.verify_assignment(&with_extra, None).unwrap();
    assert!(check.satisfied);
    assert_eq!(check.extra_variables, vec!["ghost".to_string()]);

    let mut missing = BTreeMap::new();
    missing.insert("x".to_string(), 0i64);
    let err = svc.verify_assignment(&missing, None).unwrap_err();
    assert_eq!(err.kind, ErrorKind::Input);
    assert!(err.message.contains("missing variable 'y'"));
}
