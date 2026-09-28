//! Oracle properties: truncation yields unknown; enumeration order and counts are
//! exact; language-level bit operations agree with host semantics.

use std::collections::BTreeMap;

use se_integration::common::parse;
use se_lang::interp::{self, FlowOutcome, RunOpts};
use se_verify::oracle::{enumerate_outcomes, exhaustive_oracle};

#[test]
fn truncated_domain_forces_unknown_verdict() {
    // 256 assignments, cap 16 -> truncated.
    let program = parse(
        r#"{"width":8,"inputs":[{"name":"x","low":0,"high":255}],"body":[]}"#,
    );
    let o = exhaustive_oracle(&program, 16, RunOpts::default());
    assert!(o.truncated);
    assert_eq!(o.verdict, "unknown");
    assert_eq!(o.total_assignments, 16);
}

#[test]
fn multi_input_enumeration_is_cartesian_and_ordered() {
    let program = parse(
        r#"{"width":8,"inputs":[
          {"name":"a","low":0,"high":1},{"name":"b","low":0,"high":2}],"body":[]}"#,
    );
    let (outcomes, truncated) = enumerate_outcomes(&program, 1000, RunOpts::default());
    assert!(!truncated);
    assert_eq!(outcomes.len(), 6);
    // Lexicographic by declaration order, last input varies fastest.
    let pairs: Vec<(u64, u64)> = outcomes
        .iter()
        .map(|o| (o.inputs["a"], o.inputs["b"]))
        .collect();
    assert_eq!(pairs, vec![(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)]);
    assert!(outcomes
        .iter()
        .all(|o| o.assignment_index as usize == pairs.iter().position(|p| {
            let oo = &o.inputs;
            (oo["a"], oo["b"]) == *p
        })
        .unwrap()));
}

#[test]
fn domain_with_assume_mixes_completed_and_infeasible() {
    let program = parse(
        r#"{"width":8,"inputs":[{"name":"x","low":0,"high":9}],
           "body":[{"stmt":"assume","cond":{"expr":"ult","lhs":{"expr":"var","name":"x"},"rhs":{"expr":"int","value":4}}}]}"#,
    );
    let o = exhaustive_oracle(&program, 1000, RunOpts::default());
    assert_eq!(o.verdict, "holds");
    assert_eq!(o.completed, 4);
    assert_eq!(o.infeasible_assume, 6);
    assert!(o.failures.is_empty());
}

#[test]
fn concrete_shift_modulo_width_matches_host() {
    // SMT bvshl masks shift by width; the interpreter must match.
    let program = parse(
        r#"{"width":8,"inputs":[
            {"name":"x","low":1,"high":1},{"name":"s","low":0,"high":16}],
           "body":[{"stmt":"assign","target":"r",
             "expr":{"expr":"shl","lhs":{"expr":"var","name":"x"},"rhs":{"expr":"var","name":"s"}}}]}"#,
    );
    let mut seen = BTreeMap::new();
    interp::enumerate_inputs(&program, 1000, |_, a| {
        let r = interp::run(&program, a, RunOpts::default());
        assert_eq!(r.outcome, FlowOutcome::Completed);
        seen.insert(a["s"], r.final_store["r"]);
    });
    for s in 0..=16u64 {
        let expected = 1u64.wrapping_shl((s & 7) as u32);
        assert_eq!(seen[&s], expected, "shift {s}");
    }
}
