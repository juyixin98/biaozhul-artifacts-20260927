use crate::helpers::*;
use fsm_core::QueryKind;
use fsm_fixtures::{answers, swap, SWAP_POSTCONDTION_EF};

/// The defining semantic test: parallel assignment evaluates all RHS in the
/// same pre-state, so `x := y, y := x` swaps.
#[test]
fn parallel_assignment_uses_one_prestate() {
    let sys = build(swap());
    let out = run_with(
        &sys,
        &[("swapped", QueryKind::Ef, SWAP_POSTCONDTION_EF)],
        true,
        10_000,
    );
    assert_eq!(out.status, fsm_core::RunStatus::Complete);
    assert_eq!(out.stats.states_consumed, answers::SWAP_REACHABLE_STATES);
    assert_conclusion(&out, "swapped", answers::SWAP_EXPECTED);

    let ev = evidence_of(&out, "swapped");
    assert_eq!(ev.length, answers::SWAP_PATH_LENGTH);
    let last = ev.path.last().unwrap();
    assert_eq!(last.state["x"].as_i64(), Some(answers::SWAP_AFTER.0));
    assert_eq!(last.state["y"].as_i64(), Some(answers::SWAP_AFTER.1));

    // Direct evaluator cross-check from the initial state.
    let init = vec![fsm_lang::Value::Int(1), fsm_lang::Value::Int(2)];
    let t = &sys.transitions[0];
    assert_eq!(t.assign.len(), 2, "swap transition has two assignments");
    let post = sys.apply(t, &init).unwrap();
    assert_eq!(post, vec![fsm_lang::Value::Int(2), fsm_lang::Value::Int(1)]);
}
