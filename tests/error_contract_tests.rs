//! Input / compiler error contract tests: the four error categories must stay
//! distinguishable, each with a stable code.

use wtio::compiler;
use wtio::error::ErrorKind;
use wtio::input::CheckRequest;
use wtio::solver::Verdict;

fn base_pair() -> serde_json::Value {
    serde_json::json!({
        "silent_action": "tau",
        "specification": {
            "name": "S", "initial": "s0",
            "states": ["s0"], "transitions": []
        },
        "implementation": {
            "name": "I", "initial": "i0",
            "states": ["i0"], "transitions": []
        }
    })
}

fn expect_error(json: serde_json::Value, kind: ErrorKind, code: &str) {
    let req: CheckRequest = serde_json::from_value(json).expect("parse");
    let err = compiler::compile(&req).expect_err("must fail");
    assert_eq!(err.kind, kind, "wrong category for {code}: {err}");
    assert_eq!(err.code, code, "wrong code: {err}");
}

#[test]
fn empty_action_is_input_error() {
    let mut v = base_pair();
    v["implementation"]["transitions"] = serde_json::json!([
        {"from": "i0", "action": "", "to": "i0"}
    ]);
    expect_error(v, ErrorKind::InputError, "empty_action");
}

#[test]
fn unknown_state_is_state_conflict() {
    let mut v = base_pair();
    v["implementation"]["transitions"] =
        serde_json::json!([{"from": "i0", "action": "a", "to": "ghost"}]);
    expect_error(v, ErrorKind::StateConflict, "unknown_state");

    let mut v = base_pair();
    v["implementation"]["initial"] = "ghost".into();
    expect_error(v, ErrorKind::StateConflict, "unknown_state");
}

#[test]
fn unknown_accepting_state_is_state_conflict() {
    let mut v = base_pair();
    v["implementation"]["accepting"] = serde_json::json!(["ghost"]);
    expect_error(v, ErrorKind::StateConflict, "unknown_state");
}

#[test]
fn duplicate_state_declaration_is_conflict() {
    let mut v = base_pair();
    v["implementation"]["states"] = serde_json::json!(["i0", "i0"]);
    expect_error(v, ErrorKind::StateConflict, "duplicate_state");
}

#[test]
fn silent_also_observable_is_conflict() {
    let mut v = base_pair();
    v["alphabet"] = serde_json::json!(["tau", "a"]);
    expect_error(v, ErrorKind::StateConflict, "silent_also_observable");
}

#[test]
fn duplicate_alphabet_entry_is_conflict() {
    let mut v = base_pair();
    v["alphabet"] = serde_json::json!(["a", "a"]);
    expect_error(v, ErrorKind::StateConflict, "duplicate_alphabet_action");
}

#[test]
fn action_outside_alphabet_is_input_error() {
    let mut v = base_pair();
    v["alphabet"] = serde_json::json!(["a"]);
    v["implementation"]["transitions"] =
        serde_json::json!([{"from": "i0", "action": "b", "to": "i0"}]);
    expect_error(v, ErrorKind::InputError, "action_not_in_alphabet");
}

#[test]
fn oversized_input_is_resource_exhausted_at_compile() {
    let mut v = base_pair();
    v["limits"] = serde_json::json!({"max_states_per_lts": 2});
    v["implementation"]["states"] = serde_json::json!(["i0", "i1", "i2"]);
    expect_error(v, ErrorKind::ResourceExhausted, "too_many_states");
}

#[test]
fn explicit_alphabet_alignment_is_recorded() {
    // Even when only the implementation uses 'b', an explicit alphabet keeps
    // it aligned; the shortest counterexample remains ['b'].
    let v = serde_json::json!({
        "silent_action": "tau",
        "alphabet": ["a", "b"],
        "specification": {"name": "S", "initial": "s0", "states": ["s0"],
                          "transitions": [{"from": "s0", "action": "a", "to": "s0"}]},
        "implementation": {"name": "I", "initial": "i0", "states": ["i0", "i1"],
                           "transitions": [{"from": "i0", "action": "b", "to": "i1"}]}
    });
    let req: CheckRequest = serde_json::from_value(v).unwrap();
    let pair = compiler::compile(&req).unwrap();
    assert_eq!(pair.label_names, vec!["a".to_string(), "b".to_string()]);
    let outcome = wtio::solver::check(&pair, &req.limits()).unwrap();
    assert_eq!(outcome.verdict, Verdict::NotIncluded);
    assert_eq!(outcome.counterexample.unwrap().trace, vec!["b"]);
}
