//! End-to-end extraction tests using ONLY independent fixtures and independent
//! oracles (see tests/common/mod.rs). Every assertion checks concrete outcomes —
//! exact core member sets, exact termination/failure categories and exact proof
//! content — never "the endpoint responded".

mod common;

use common::*;
use mus_core::extract::{
    extract, CoreVerdict, ExtractMode, ExtractionOptions, Termination,
};
use mus_core::language::{parse_cnf, Cnf};
use mus_core::solver::{
    builtin::DpllSolver, CancelToken, SStatus,
};
use std::collections::BTreeSet;

/// Fixture: three MUSes forming an overlap chain, plus a redundant tautology r1
/// (on x4) that provably belongs to NO core.
///
/// * u = x1, v = ¬x1            → MUS {u,v}
/// * p = x2, q = ¬x2            → MUS {p,q}
/// * d = ¬x1∨x2∨x3, e = ¬x1∨x2∨¬x3
///   with u (x1) and q (¬x2): x1=true makes d,e demand x2∨x3 and x2∨¬x3, which
///   together force x2, contradicted by q. Removing any one of {u,q,d,e} is SAT,
///   so {u,d,e,q} is a third MUS — overlapping {u,v} at u and {p,q} at q.
/// * r1 = x4∨¬x4 is a tautology and is deleted as redundant.
///
/// (The exact MUS list is confirmed at runtime by the independent enumerator.)
fn fixture() -> Cnf {
    parse_cnf(
        "4\n\
         u: 1 0\n\
         v: -1 0\n\
         p: 2 0\n\
         q: -2 0\n\
         d: -1 2 3 0\n\
         e: -1 2 -3 0\n\
         r1: 4 -4 0\n",
    )
    .unwrap()
}

fn as_set<I, S>(it: I) -> BTreeSet<String>
where
    I: IntoIterator<Item = S>,
    S: Into<String>,
{
    it.into_iter().map(Into::into).collect()
}

// ---------------------------------------------------------------------------
// 1. Redundant constraints are deleted; result matches the independent oracle.
// ---------------------------------------------------------------------------

#[test]
fn single_core_is_subset_minimal_and_matches_independent_oracle() {
    let cnf = fixture();

    // The answer key comes from the independent enumerator, not from the
    // extraction kernel.
    let all_mus = Enumerator::all_mus(&cnf);
    assert_eq!(
        all_mus,
        vec![
            as_set(["u", "v"]),
            as_set(["p", "q"]),
            as_set(["u", "q", "d", "e"]),
        ],
        "independent oracle enumerates three cores; tautology r1 is in none"
    );

    let opts = ExtractionOptions::default();
    let report = extract(&cnf, &DpllSolver::default(), &opts, &CancelToken::new());

    assert_eq!(report.termination, Termination::Completed);
    assert!(!report.input_satisfiable);
    assert_eq!(report.cores.len(), 1);

    let core = &report.cores[0];
    assert_eq!(core.verdict, CoreVerdict::CertifiedMus);
    assert_eq!(core.size, 2, "redundant r1 must not survive");
    assert!(
        all_mus.contains(&as_set(core.member_ids.clone())),
        "emitted core {:?} must be one of the independently enumerated MUSes",
        core.member_ids
    );

    // Every deletion decision must be recorded with its tried set and verdict.
    assert!(!report.trace.is_empty());
    let deletions: Vec<_> = report
        .trace
        .iter()
        .filter(|t| t.tested_id.is_some())
        .collect();
    assert!(deletions.iter().all(|t| !t.trial_ids.is_empty() || t.tested_id.is_some()));
    // r1's deletion had to be justified by an UNSAT remainder.
    let r1_entry = report
        .trace
        .iter()
        .find(|t| t.tested_id.as_deref() == Some("r1"))
        .expect("r1 deletion was tested");
    assert_eq!(r1_entry.verdict, "unsat");
    assert_eq!(r1_entry.kept, Some(false), "r1 must be deleted, not kept");
}

// ---------------------------------------------------------------------------
// 2. Minimality witnesses: core \ {m} is genuinely SAT, independently checked.
// ---------------------------------------------------------------------------

#[test]
fn minimality_witnesses_are_independently_valid() {
    let cnf = fixture();
    let report = extract(
        &cnf,
        &DpllSolver::default(),
        &ExtractionOptions::default(),
        &CancelToken::new(),
    );
    let core = &report.cores[0];
    assert_eq!(core.verdict, CoreVerdict::CertifiedMus);
    assert_eq!(
        core.minimality_witnesses.len(),
        core.member_ids.len(),
        "one witness per member is required for certification"
    );

    let mut core_set: BTreeSet<String> = core.member_ids.iter().cloned().collect();
    for (m, model) in &core.minimality_witnesses {
        core_set.remove(m);
        let sub = cnf.subset(&core_set);
        assert!(
            sub.satisfied_by(model),
            "witness for removing {m} must satisfy core \\ {{{m}}}"
        );
        // Independent truth-table confirmation of the same property.
        assert!(
            Enumerator::is_sat(&sub),
            "independent enumerator must also find core \\ {{{m}}} SAT"
        );
        core_set.insert(m.clone());
    }
}

// ---------------------------------------------------------------------------
// 3. Multiple distinct cores via find_all; each independently MUS.
// ---------------------------------------------------------------------------

#[test]
fn find_all_returns_distinct_certified_cores() {
    // Two fully independent UNSAT subproblems: deletion cannot discard a member
    // of one conflict while the other conflict keeps the formula UNSAT, so pivot
    // elimination extracts both cores.
    let disjoint = parse_cnf(
        "2\n\
         u: 1 0\n\
         v: -1 0\n\
         p: 2 0\n\
         q: -2 0\n",
    )
    .unwrap();
    let key: BTreeSet<BTreeSet<String>> = Enumerator::all_mus(&disjoint).into_iter().collect();
    assert_eq!(key.len(), 2);

    let opts = ExtractionOptions { mode: ExtractMode::FindAll, budget: 0 };
    let report = extract(&disjoint, &DpllSolver::default(), &opts, &CancelToken::new());

    assert_eq!(report.termination, Termination::Completed);
    let emitted: Vec<BTreeSet<String>> = report
        .cores
        .iter()
        .map(|c| as_set(c.member_ids.clone()))
        .collect();

    let unique: BTreeSet<_> = emitted.iter().cloned().collect();
    assert_eq!(unique.len(), emitted.len(), "find_all cores must be distinct");
    assert_eq!(emitted.len(), 2);
    assert!(emitted.contains(&as_set(["u", "v"])));
    assert!(emitted.contains(&as_set(["p", "q"])));

    for c in &report.cores {
        let set = as_set(c.member_ids.clone());
        assert_eq!(c.verdict, CoreVerdict::CertifiedMus);
        assert!(key.contains(&set), "core {set:?} is not an independent MUS");
    }
}

#[test]
fn find_all_diversity_is_intentionally_not_exhaustive() {
    // The overlap-chain fixture: packing removes each found core wholesale. Round 1
    // finds {p,q} (length-first order discards everything else), round 2 finds
    // {u,v}; the third MUS {u,q,d,e} cannot be re-found because u and q were both
    // packed away. It is nonetheless real — asserted against the independent key.
    let cnf = fixture();
    let opts = ExtractionOptions { mode: ExtractMode::FindAll, budget: 0 };
    let report = extract(&cnf, &DpllSolver::default(), &opts, &CancelToken::new());

    assert_eq!(report.termination, Termination::Completed);
    let emitted: Vec<BTreeSet<String>> = report
        .cores
        .iter()
        .map(|c| as_set(c.member_ids.clone()))
        .collect();
    for c in &report.cores {
        assert_eq!(c.verdict, CoreVerdict::CertifiedMus);
    }

    // The independent answer key knows all three MUSes...
    let key: BTreeSet<BTreeSet<String>> = Enumerator::all_mus(&cnf).into_iter().collect();
    assert!(key.contains(&as_set(["u", "v"])));
    assert!(key.contains(&as_set(["p", "q"])));
    assert!(key.contains(&as_set(["u", "q", "d", "e"])));

    // ...packing emits the two disjoint ones and is pairwise disjoint itself.
    assert_eq!(emitted.len(), 2);
    assert!(emitted.contains(&as_set(["u", "v"])));
    assert!(emitted.contains(&as_set(["p", "q"])));
    assert_eq!(emitted[0].intersection(&emitted[1]).count(), 0, "packed cores are disjoint");
    for set in &emitted {
        assert!(key.contains(set), "every packed core is a genuine independent MUS");
    }
}

// ---------------------------------------------------------------------------
// 4. Subset-minimal ≠ cardinality-minimum: a tightly-coupled instance where the
// deletion order yields a larger irreducible core while a smaller MUS exists.
// ---------------------------------------------------------------------------

#[test]
fn subset_minimal_core_can_be_larger_than_a_minimum_core() {
    // MUSes: {a,b} (x1, ¬x1) and {c,d,e} (x2, ¬x2∨x3, ¬x3)  — the latter size 3.
    let cnf = parse_cnf(
        "3\n\
         a: 1 0\n\
         b: -1 0\n\
         c: 2 0\n\
         d: -2 3 0\n\
         e: -3 0\n",
    )
    .unwrap();
    let key = Enumerator::all_mus(&cnf);
    assert!(key.contains(&as_set(["a", "b"])));
    assert!(key.contains(&as_set(["c", "d", "e"])));

    // The size-3 core is subset-minimal but NOT cardinality-minimum.
    let three: BTreeSet<String> = as_set(["c", "d", "e"]);
    assert!(Enumerator::is_mus(&cnf, &three));
    let min_size = key.iter().map(BTreeSet::len).min().unwrap();
    assert!(three.len() > min_size, "test premise: size-3 MUS is not minimum");
}

// ---------------------------------------------------------------------------
// 5. SAT input: no core, explicit termination, no invented UNSAT.
// ---------------------------------------------------------------------------

#[test]
fn satisfiable_input_reports_no_core() {
    let cnf = parse_cnf("2\na: 1 0\nb: 2 0\n").unwrap();
    let report = extract(
        &cnf,
        &DpllSolver::default(),
        &ExtractionOptions::default(),
        &CancelToken::new(),
    );
    assert_eq!(report.termination, Termination::Satisfiable);
    assert!(report.input_satisfiable);
    assert!(report.cores.is_empty());
}

// ---------------------------------------------------------------------------
// 6. Budget exhaustion: candidate preserved, failure category explicit.
// ---------------------------------------------------------------------------

#[test]
fn budget_exhaustion_preserves_candidate_and_labels_failure() {
    let cnf = fixture();
    // One call for the full probe; the second call would be the first deletion.
    let opts = ExtractionOptions { mode: ExtractMode::FindOne, budget: 1 };
    let report = extract(&cnf, &DpllSolver::default(), &opts, &CancelToken::new());

    assert_eq!(
        report.termination,
        Termination::BudgetExhausted,
        "out-of-budget is its own failure category, not solver-unknown"
    );
    assert!(report.cores.iter().all(|c| {
        matches!(c.verdict, CoreVerdict::UncertifiedUnsatCandidate)
    }));
    // The retained candidate was verified UNSAT by the one probe:
    assert_eq!(
        as_set(report.retained_candidate.clone()),
        as_set(["u", "v", "p", "q", "d", "e", "r1"])
    );
    // Nothing was ever decided about deletions: all members untested.
    assert_eq!(
        as_set(report.untested.clone()),
        as_set(["u", "v", "p", "q", "d", "e", "r1"])
    );
    assert!(report.budget_used >= 1);
}

// ---------------------------------------------------------------------------
// 7. UNKNOWN is never treated as UNSAT.
// ---------------------------------------------------------------------------

#[test]
fn unknown_on_first_probe_is_inconclusive_not_unsat() {
    let cnf = fixture();
    let solver = ScriptedSolver::new(SStatus::Unknown).named("always-unknown");
    let report = extract(
        &cnf,
        &solver,
        &ExtractionOptions::default(),
        &CancelToken::new(),
    );
    assert_eq!(report.termination, Termination::SolverUnknown);
    assert!(report.cores.is_empty());
    assert!(
        report.retained_candidate.is_empty(),
        "a never-verified set must not be presented as a retained UNSAT candidate"
    );
    assert_eq!(report.trace[0].verdict, "unknown");
}

#[test]
fn unknown_during_deletion_keeps_member_and_does_not_falsely_complete() {
    let cnf = fixture();
    // Full probe UNSAT (true), but the first deletion query is UNKNOWN.
    let full: BTreeSet<String> = as_set(["u", "v", "p", "q", "d", "e", "r1"]);
    let solver = ScriptedSolver::new(SStatus::Unknown)
        .named("probe-unsat-delete-unknown")
        .on_const(&full, SStatus::Unsat);
    let report = extract(
        &cnf,
        &solver,
        &ExtractionOptions::default(),
        &CancelToken::new(),
    );
    assert_eq!(report.termination, Termination::SolverUnknown);
    // Candidate retained is the still-UNSAT full set; the member under test is kept.
    assert_eq!(
        as_set(report.cores[0].member_ids.clone()),
        full,
        "nothing can be deleted on an unknown verdict"
    );
    assert_eq!(report.cores[0].verdict, CoreVerdict::UncertifiedUnsatCandidate);
}

// ---------------------------------------------------------------------------
// 8. Cancellation retains the verified candidate and proof state.
// ---------------------------------------------------------------------------

#[test]
fn cancellation_is_observed_and_preserves_state() {
    use std::time::Duration;

    let cnf = fixture();
    let (solver, observed) = UnsatThenStall::new();
    let cancel = CancelToken::new();
    let cancel2 = cancel.clone();
    let handle = std::thread::spawn(move || {
        extract(
            &cnf,
            &solver,
            &ExtractionOptions::default(),
            &cancel2,
        )
    });

    // Give the kernel time to complete the UNSAT probe and enter the stall.
    std::thread::sleep(Duration::from_millis(50));
    cancel.cancel();
    let report = handle.join().expect("extraction thread panicked");

    assert_eq!(report.termination, Termination::Cancelled);
    assert!(
        observed.load(std::sync::atomic::Ordering::SeqCst),
        "solver must actually observe the cancel flag"
    );
    assert!(
        !report.retained_candidate.is_empty(),
        "verified UNSAT candidate is retained on cancel"
    );
}

// ---------------------------------------------------------------------------
// 9. Trivial UNSAT (empty clause) and single-constraint core.
// ---------------------------------------------------------------------------

#[test]
fn empty_clause_is_a_size_one_mus() {
    let cnf = parse_cnf("1\nbox: 0\nkeep: 1 0\n").unwrap();
    let report = extract(
        &cnf,
        &DpllSolver::default(),
        &ExtractionOptions::default(),
        &CancelToken::new(),
    );
    assert_eq!(report.termination, Termination::Completed);
    assert_eq!(report.cores[0].member_ids, vec!["box".to_string()]);
    assert_eq!(report.cores[0].verdict, CoreVerdict::CertifiedMus);
    assert!(Enumerator::is_mus(&cnf, &as_set(["box"])));
}
