//! Independent evidence-verification tests. The oracle is always the test
//! enumerator (independent code path), never the kernel being checked.

mod common;

use common::*;
use mus_core::extract::{
    CoreVerdict, ExtractionOptions, FoundCore, Termination,
};
use mus_core::language::{parse_cnf, Cnf, Model};
use mus_core::solver::{builtin::DpllSolver, CancelToken, SStatus};
use std::collections::{BTreeMap, BTreeSet};

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

#[test]
fn genuine_certified_core_passes_independent_verifier() {
    let cnf = fixture();
    let report = mus_core::extract::extract(
        &cnf,
        &DpllSolver::default(),
        &ExtractionOptions::default(),
        &CancelToken::new(),
    );
    assert_eq!(report.termination, Termination::Completed);

    let vr = mus_core::verify::verify_report(&cnf, &report, &EnumSolver, 0, 1000);
    assert!(
        vr.all_certified,
        "independently enumerated MUS must certify; failures: {:?}",
        vr.cores.iter().map(|c| (&c.result, &c.member_failures)).collect::<Vec<_>>()
    );
    assert!(vr.trace_audit.iter().all(|a| a.ok), "every logged decision must match the oracle");
}

#[test]
fn a_sat_set_pretending_to_be_a_core_is_rejected_as_not_unsat() {
    let cnf = fixture();
    let fake = FoundCore {
        member_ids: vec!["r1".to_string(), "u".to_string()],
        size: 2,
        verdict: CoreVerdict::CertifiedMus,
        minimality_witnesses: BTreeMap::new(),
    };
    // Sanity: the faked set is genuinely SAT per the independent enumerator
    // (x4=true satisfies the tautology, x1=true satisfies u).
    let set: BTreeSet<String> = ["r1", "u"].into_iter().map(str::to_string).collect();
    assert!(Enumerator::is_sat(&cnf.subset(&set)));

    let v = mus_core::verify::verify_core(&cnf, &fake, &EnumSolver, 0);
    assert_eq!(v.result, mus_core::verify::CoreCheck::NotUnsat);
}

#[test]
fn non_minimal_set_is_rejected() {
    let cnf = fixture();
    // {p,q} is a MUS; adding the tautology r1 keeps UNSAT but breaks minimality.
    let set: BTreeSet<String> = ["p", "q", "r1"].into_iter().map(str::to_string).collect();
    assert!(Enumerator::is_unsat(&cnf.subset(&set)));
    let nonminimal = FoundCore {
        member_ids: set.into_iter().collect(),
        size: 3,
        verdict: CoreVerdict::CertifiedMus,
        minimality_witnesses: BTreeMap::new(),
    };
    let v = mus_core::verify::verify_core(&cnf, &nonminimal, &EnumSolver, 0);
    assert_eq!(v.result, mus_core::verify::CoreCheck::NotMinimal);
    assert!(
        v.member_failures.keys().any(|k| k == "r1"),
        "removing the redundant member must be identified"
    );
}

#[test]
fn a_bad_witness_is_detected_not_trusted() {
    let cnf = fixture();
    // Real MUS {u,v}: u=x1, v=¬x1. Removing u leaves {v}, satisfied only by
    // x1=false; the recorded "witness" for u assigns x1=true and falsifies it.
    let mut witnesses = BTreeMap::new();
    witnesses.insert(
        "u".to_string(),
        Model(vec![true, true /*x1 wrongly true; falsifies v=¬x1*/, true, true, true]),
    );
    witnesses.insert(
        "v".to_string(),
        Model(vec![true, true /*x1=true correctly satisfies {u}=x1*/, true, true, true]),
    );
    let core = FoundCore {
        member_ids: vec!["u".to_string(), "v".to_string()],
        size: 2,
        verdict: CoreVerdict::CertifiedMus,
        minimality_witnesses: witnesses,
    };
    let v = mus_core::verify::verify_core(&cnf, &core, &EnumSolver, 0);
    assert_eq!(v.result, mus_core::verify::CoreCheck::BadWitness);
}

#[test]
fn unknown_oracle_makes_verification_inconclusive_not_certified() {
    let cnf = fixture();
    let core = FoundCore {
        member_ids: vec!["u".to_string(), "v".to_string()],
        size: 2,
        verdict: CoreVerdict::CertifiedMus,
        minimality_witnesses: BTreeMap::new(),
    };
    let v = mus_core::verify::verify_core(&cnf, &core, &LyingSolver::new(SStatus::Unknown), 0);
    assert_eq!(v.result, mus_core::verify::CoreCheck::Inconclusive);
}

#[test]
fn trace_audit_catches_a_kernel_that_lied_about_unsat() {
    use mus_core::extract::{extract, ExtractionReport};

    let cnf = fixture();
    // A kernel that calls everything UNSAT will happily "extract a core" from
    // anything; the independent trace audit must expose the contradiction.
    let liar = LyingSolver::new(SStatus::Unsat);
    let report: ExtractionReport = extract(
        &cnf,
        &liar,
        &ExtractionOptions::default(),
        &CancelToken::new(),
    );
    // The lying run even logs an empty-trial UNSAT at the end of its walk —
    // independently that empty formula is SAT.
    let vr = mus_core::verify::verify_report(&cnf, &report, &EnumSolver, 0, 1000);
    assert!(
        !vr.all_certified || vr.trace_audit.iter().any(|a| !a.ok),
        "independent verification must fail a report fabricated by a lying kernel"
    );
    assert!(
        vr.trace_audit.iter().any(|a| !a.ok),
        "at least one logged UNSAT/SAT decision must disagree with the enumerator"
    );
}
