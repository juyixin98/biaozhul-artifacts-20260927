//! Solver kernel tests: concrete verdicts, shortest-trace assertions and
//! cross-checking against the independent brute-force oracle.
//!
//! The oracle (`wtio::oracle`) shares none of the solver's search code, so
//! agreement here is an actual second opinion rather than self-certification.

use wtio::compiler;
use wtio::input::CheckRequest;
use wtio::oracle;
use wtio::solver::{self, Verdict};
use wtio::verifier;

fn load(name: &str) -> CheckRequest {
    let path = format!("fixtures/{name}.json");
    let bytes = std::fs::read(&path).unwrap_or_else(|e| panic!("read {path}: {e}"));
    serde_json::from_slice(&bytes).expect("fixture must be valid JSON")
}

fn verdict_of(req: &CheckRequest) -> (Verdict, Option<Vec<String>>) {
    let pair = compiler::compile(req).expect("compile");
    let outcome = solver::check(&pair, &req.limits()).expect("check");
    (
        outcome.verdict,
        outcome.counterexample.map(|c| c.trace),
    )
}

#[test]
fn hidden_internal_steps_are_ignored() {
    let req = load("hidden_internal_steps_included");
    let (verdict, ce) = verdict_of(&req);
    assert_eq!(verdict, Verdict::Included, "tau prefix must be invisible");
    assert!(ce.is_none());
}

#[test]
fn erroneous_extra_output_gives_shortest_trace() {
    let req = load("erroneous_extra_output");
    let pair = compiler::compile(&req).unwrap();
    let outcome = solver::check(&pair, &req.limits()).unwrap();
    assert_eq!(outcome.verdict, Verdict::NotIncluded);
    let ce = outcome.counterexample.expect("counterexample");
    assert_eq!(ce.trace, vec!["x".to_string()], "shortest divergence is [x]");

    // The replay must pass through the hidden internal step.
    let replay = &ce.implementation_replay;
    assert_eq!(replay.start_state, "i0");
    assert_eq!(replay.edge_count, 2, "one tau + one observable edge");
    assert_eq!(replay.hops[0].before_tau.len(), 1);
    assert_eq!(replay.hops[0].before_tau[0].action, "tau");
    assert_eq!(replay.hops[0].observable_edge.action, "x");
    assert_eq!(replay.accepting_state, "i2");

    // Independent acceptance difference on both sides.
    assert!(verifier::trace_accepted(&pair, false, &ce.trace).unwrap());
    assert!(!verifier::trace_accepted(&pair, true, &ce.trace).unwrap());
    let v = verifier::verify_counterexample(&pair, &ce.implementation_replay, &ce.trace).unwrap();
    assert!(v.confirmed, "independent verification must confirm: {:?}", v.problems);
}

#[test]
fn unreachable_branches_cannot_be_traced() {
    for case in ["unreachable_branch_ignored", "tau_unreachable_branch_ignored"] {
        let req = load(case);
        let (verdict, ce) = verdict_of(&req);
        assert_eq!(verdict, Verdict::Included, "{case}: unreachable 'bad' output must not count");
        assert!(ce.is_none(), "{case}");

        // And explicitly: 'bad' is not a weak trace of the implementation.
        let pair = compiler::compile(&req).unwrap();
        assert!(!verifier::trace_accepted(&pair, false, &["bad".to_string()]).unwrap());
    }
}

#[test]
fn same_single_step_actions_are_not_equivalent() {
    // The classic mistake: both states have exactly one edge labeled 'a', so a
    // checker that collapses states by identical outgoing actions misses the
    // acceptance difference. The subset construction must not.
    let req = load("acceptance_diff_via_marks");
    let pair = compiler::compile(&req).unwrap();
    let outcome = solver::check(&pair, &req.limits()).unwrap();
    assert_eq!(outcome.verdict, Verdict::NotIncluded);
    let ce = outcome.counterexample.unwrap();
    assert_eq!(ce.trace, vec!["a".to_string()]);

    // Macro-path acceptance difference at the failing node.
    assert_eq!(ce.impl_path.last().unwrap(), &vec!["i1".to_string()]);
    assert!(ce.spec_path.last().unwrap().is_empty() || {
        // spec weak-post after 'a' is {s1}, non-accepting.
        ce.spec_path.last().unwrap() == &vec!["s1".to_string()]
    });

    let v = verifier::verify_counterexample(&pair, &ce.implementation_replay, &ce.trace).unwrap();
    assert!(v.confirmed);
    assert!(v.implementation_accepts_trace);
    assert!(!v.specification_accepts_trace);
}

#[test]
fn bfs_returns_the_shortest_counterexample() {
    let req = load("shortest_counterexample");
    let pair = compiler::compile(&req).unwrap();
    let outcome = solver::check(&pair, &req.limits()).unwrap();
    assert_eq!(outcome.verdict, Verdict::NotIncluded);
    let ce = outcome.counterexample.unwrap();
    assert_eq!(ce.trace, vec!["a".to_string(), "x".to_string()]);
    // No length-1 divergence exists: oracle cross-check.
    let o = oracle::brute_force_diff(&pair, 4);
    assert!(o.impl_yes_spec_no);
    let oracle_word: Vec<String> = o
        .shortest_diff
        .iter()
        .map(|l| pair.label_name(*l).to_string())
        .collect();
    assert_eq!(oracle_word, ce.trace, "kernel and independent oracle disagree");
}

#[test]
fn empty_trace_counterexample() {
    let req = load("epsilon_acceptance_diff");
    let pair = compiler::compile(&req).unwrap();
    let outcome = solver::check(&pair, &req.limits()).unwrap();
    assert_eq!(outcome.verdict, Verdict::NotIncluded);
    let ce = outcome.counterexample.unwrap();
    assert!(ce.trace.is_empty(), "divergence on the empty word");
    assert_eq!(ce.implementation_replay.edge_count, 0);
    let v = verifier::verify_counterexample(&pair, &ce.implementation_replay, &ce.trace).unwrap();
    assert!(v.confirmed);
}

#[test]
fn vending_machine_free_coffee_through_tau() {
    // End-to-end style model: the implementation reaches an 'armed' state only
    // via two internal steps (boot~>armed) and then serves coffee without a
    // coin. Shortest counterexample ['coffee'] must replay through both taus.
    let req = load("vending_machine");
    let pair = compiler::compile(&req).unwrap();
    let outcome = solver::check(&pair, &req.limits()).unwrap();
    assert_eq!(outcome.verdict, Verdict::NotIncluded);
    let ce = outcome.counterexample.unwrap();
    assert_eq!(ce.trace, vec!["coffee".to_string()]);

    let replay = &ce.implementation_replay;
    assert_eq!(replay.start_state, "boot");
    assert_eq!(replay.accepting_state, "done");
    assert_eq!(replay.edge_count, 3, "two taus then coffee");
    let taus: Vec<(String, String)> = replay.hops[0]
        .before_tau
        .iter()
        .map(|e| (e.state.clone(), e.target.clone()))
        .collect();
    assert_eq!(
        taus,
        vec![
            ("boot".to_string(), "idle".to_string()),
            ("idle".to_string(), "armed".to_string())
        ]
    );

    // After 'coffee' the spec macro-set is empty, the impl macro-set is done.
    assert!(ce.spec_path.last().unwrap().is_empty());
    assert_eq!(ce.impl_path.last().unwrap(), &vec!["done".to_string()]);

    let v = verifier::verify_counterexample(&pair, &replay, &ce.trace).unwrap();
    assert!(v.confirmed, "{:?}", v.problems);
}

#[test]
fn nondeterministic_but_included() {
    let req = load("nondeterministic_included");
    let (verdict, _) = verdict_of(&req);
    assert_eq!(verdict, Verdict::Included);
}

#[test]
fn resource_exhaustion_is_unknown_not_false() {
    let req = load("resource_exhausted_unknown");
    let pair = compiler::compile(&req).unwrap();
    let outcome = solver::check(&pair, &req.limits()).unwrap();
    assert_eq!(
        outcome.verdict,
        Verdict::Unknown,
        "hitting a search budget must yield unknown"
    );
    assert!(outcome.counterexample.is_none(), "no fabricated counterexample");
    match outcome.unknown.unwrap() {
        solver::UnknownReason::PairLimit { limit } => assert_eq!(limit, 3),
        other => panic!("expected pair_limit, got {other:?}"),
    }
}

#[test]
fn kernel_matches_oracle_on_all_fixtures() {
    // Enumerative agreement check for every fixed fixture up to depth 5.
    for name in [
        "hidden_internal_steps_included",
        "erroneous_extra_output",
        "unreachable_branch_ignored",
        "tau_unreachable_branch_ignored",
        "acceptance_diff_via_marks",
        "shortest_counterexample",
        "nondeterministic_included",
    ] {
        let req = load(name);
        let pair = compiler::compile(&req).unwrap();
        let outcome = solver::check(&pair, &req.limits()).unwrap();
        let oracle_v = oracle::brute_force_diff(&pair, 5);
        match outcome.verdict {
            Verdict::Included => {
                assert!(
                    !oracle_v.impl_yes_spec_no,
                    "{name}: kernel says included but oracle found {:?}",
                    oracle_v.shortest_diff
                );
            }
            Verdict::NotIncluded => {
                let ce = outcome.counterexample.unwrap();
                assert!(oracle_v.impl_yes_spec_no, "{name}: oracle disagrees");
                let ow: Vec<String> = oracle_v
                    .shortest_diff
                    .iter()
                    .map(|l| pair.label_name(*l).to_string())
                    .collect();
                assert_eq!(ow, ce.trace, "{name}: shortest word mismatch");
            }
            Verdict::Unknown => {}
        }
    }
}
