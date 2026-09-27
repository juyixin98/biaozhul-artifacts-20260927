use crate::helpers::*;
use fsm_core::QueryKind;
use fsm_fixtures::{answers, mutex_bad, mutex_safe, MUTEX_INVARIANT};

#[test]
fn mutex_safe_invariant_holds_with_exact_state_count() {
    let sys = build(mutex_safe());
    let out = run_with(
        &sys,
        &[("mutex", QueryKind::Ag, MUTEX_INVARIANT)],
        true,
        10_000,
    );
    assert_eq!(out.status, fsm_core::RunStatus::Complete);
    assert_conclusion(&out, "mutex", answers::MUTEX_SAFE_INVARIANT);
    assert_conclusion(&out, "deadlock", answers::MUTEX_SAFE_DEADLOCK);
    assert_eq!(
        out.stats.states_consumed,
        answers::MUTEX_SAFE_REACHABLE_STATES
    );
    assert_eq!(
        out.stats.terminal_states,
        answers::MUTEX_SAFE_TERMINAL_STATES
    );
    assert_eq!(out.stats.deadlocked_states, 0);
    assert!(!out.deadlock_found);
}

#[test]
fn mutex_bad_invariant_violated_shortest_path_is_two() {
    let sys = build(mutex_bad());
    let out = run_with(
        &sys,
        &[("mutex", QueryKind::Ag, MUTEX_INVARIANT)],
        true,
        10_000,
    );
    assert_eq!(out.status, fsm_core::RunStatus::Complete);
    assert_conclusion(&out, "mutex", answers::MUTEX_BAD_INVARIANT);
    assert_eq!(
        out.stats.states_consumed,
        answers::MUTEX_BAD_REACHABLE_STATES
    );

    let ev = evidence_of(&out, "mutex");
    assert_eq!(ev.kind, fsm_core::EvidenceKind::AgViolation);
    assert_eq!(ev.length, answers::MUTEX_BAD_CEX_LENGTH);
    assert_eq!(ev.path.len(), answers::MUTEX_BAD_CEX_LENGTH + 1);
}

/// Hand replay of every counterexample step: compare the kernel witness
/// against the independently written step table in `answers`.
#[test]
fn mutex_bad_counterexample_replayed_step_by_step() {
    let sys = build(mutex_bad());
    let out = run_with(
        &sys,
        &[("mutex", QueryKind::Ag, MUTEX_INVARIANT)],
        false,
        10_000,
    );
    let ev = evidence_of(&out, "mutex");
    assert_eq!(ev.path.len(), answers::MUTEX_BAD_CEX.len());

    for (step, (vals, fired)) in ev.path.iter().zip(answers::MUTEX_BAD_CEX) {
        // state values in declared order in1,in2,locked
        let got = [
            step.state["in1"].as_bool().unwrap().to_string(),
            step.state["in2"].as_bool().unwrap().to_string(),
            step.state["locked"].as_bool().unwrap().to_string(),
        ];
        assert_eq!(got, *vals, "state mismatch at step {}", step.index);
        match fired {
            None => assert!(step.fired.is_none(), "root must have no fired"),
            Some(name) => assert_eq!(step.fired.as_deref(), Some(*name)),
        }
    }

    // Independently replay each edge with the language semantics and assert
    // the invariant is false only at the final state.
    let mut current = vec![
        fsm_lang::Value::Bool(false),
        fsm_lang::Value::Bool(false),
        fsm_lang::Value::Bool(false),
    ];
    let mut inv = fsm_lang::parser::parse_expr(MUTEX_INVARIANT).unwrap();
    fsm_lang::eval::check_boolean_predicate(&sys, &mut inv).unwrap();
    for (i, (_vals, fired)) in answers::MUTEX_BAD_CEX.iter().enumerate().skip(1) {
        let name = fired.unwrap();
        let t = sys
            .transitions
            .iter()
            .find(|t| t.name == name)
            .unwrap_or_else(|| panic!("transition {name}"));
        assert!(sys.guard_holds(t, &current).unwrap(), "guard at step {i}");
        current = sys.apply(t, &current).unwrap();
        let holds = fsm_lang::eval::eval(&sys, &inv, &current).unwrap();
        let is_last = i == answers::MUTEX_BAD_CEX.len() - 1;
        assert_eq!(
            holds,
            fsm_lang::Value::Bool(!is_last),
            "invariant must hold until the final violating step"
        );
    }
}

#[test]
fn mutex_bad_witness_accepted_by_independent_verifier() {
    let sys = build(mutex_bad());
    let out = run_with(
        &sys,
        &[("mutex", QueryKind::Ag, MUTEX_INVARIANT)],
        false,
        10_000,
    );
    let input = evidence_to_input(
        out.properties[0].evidence.as_ref().unwrap(),
        Some(MUTEX_INVARIANT),
    );
    let report = fsm_verify::verify(&sys, &input);
    assert!(report.accepted, "verifier failures: {:?}", report.failures);
    assert_eq!(report.replayed_steps, answers::MUTEX_BAD_CEX_LENGTH);
}

/// Convert kernel evidence into the independently-deserialized verifier
/// input shape.
pub(crate) fn evidence_to_input(
    ev: &fsm_core::Evidence,
    expr: Option<&str>,
) -> fsm_verify::EvidenceInput {
    use fsm_verify::{EvidenceInput, EvidenceKindInput, StepInput};
    let kind = match ev.kind {
        fsm_core::EvidenceKind::AgViolation => EvidenceKindInput::AgViolation,
        fsm_core::EvidenceKind::EfReachable => EvidenceKindInput::EfReachable,
        fsm_core::EvidenceKind::Deadlock => EvidenceKindInput::Deadlock,
    };
    EvidenceInput {
        kind,
        expr: expr.map(String::from),
        length: Some(ev.length),
        path: ev
            .path
            .iter()
            .map(|s| StepInput {
                state: s.state.clone(),
                fired: s.fired.clone(),
            })
            .collect(),
    }
}
