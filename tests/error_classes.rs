//! Tests that the four failure classes are distinguishable by `ErrorKind`
//! and stable `reason` codes, at the kernel boundary.

mod common;

use bounded_monitor::error::ErrorKind;
use bounded_monitor::kernel::{Limits, Monitor};
use bounded_monitor::language::{
    ConsumePolicy, EventPattern, Predicate, ResponseRuleDef, RuleDef, Ruleset, Step, SustainRuleDef,
    SustainScope,
};
use common::{log_event, step};

fn rs_with(rule: RuleDef) -> Ruleset {
    Ruleset { id: "err".to_string(), version: "1".to_string(), rules: vec![rule] }
}

#[test]
fn input_errors_bad_ruleset_constants_and_predicates() {
    // within_steps = 0
    let bad = rs_with(RuleDef::Response(ResponseRuleDef {
        id: "r".to_string(),
        trigger: EventPattern { event_type: "t".to_string(), where_: None },
        response: EventPattern { event_type: "r".to_string(), where_: None },
        within_steps: 0,
        consume: ConsumePolicy::AllMatching,
        correlation_key: None,
    }));
    let e = Monitor::new(bad, Limits::default()).unwrap_err();
    assert_eq!(e.kind, ErrorKind::InputError);
    assert_eq!(e.reason, "zero_window");
    log_event("err-input", "input", "zero_window", e.detail.clone());

    // duplicate rule ids
    let dup = Ruleset {
        id: "err".to_string(),
        version: "1".to_string(),
        rules: vec![
            RuleDef::Response(ResponseRuleDef {
                id: "same".to_string(),
                trigger: EventPattern { event_type: "t".to_string(), where_: None },
                response: EventPattern { event_type: "r".to_string(), where_: None },
                within_steps: 1,
                consume: ConsumePolicy::AllMatching,
                correlation_key: None,
            }),
            RuleDef::Sustain(SustainRuleDef {
                id: "same".to_string(),
                condition: Predicate::Bool { path: "x".to_string() },
                duration_steps: 1,
                scope: SustainScope::Always,
            }),
        ],
    };
    let e = Monitor::new(dup, Limits::default()).unwrap_err();
    assert_eq!(e.kind, ErrorKind::InputError);
    assert_eq!(e.reason, "duplicate_rule_id");

    // empty and/or
    let empty_and = rs_with(RuleDef::Sustain(SustainRuleDef {
        id: "s".to_string(),
        condition: Predicate::And { all: vec![] },
        duration_steps: 2,
        scope: SustainScope::Always,
    }));
    let e = Monitor::new(empty_and, Limits::default()).unwrap_err();
    assert_eq!(e.kind, ErrorKind::InputError);
    assert_eq!(e.reason, "empty_and");
}

#[test]
fn state_conflicts_step_order_and_closed_monitor() {
    let good = rs_with(RuleDef::Response(ResponseRuleDef {
        id: "r".to_string(),
        trigger: EventPattern { event_type: "t".to_string(), where_: None },
        response: EventPattern { event_type: "r".to_string(), where_: None },
        within_steps: 2,
        consume: ConsumePolicy::AllMatching,
        correlation_key: None,
    }));
    let mut m = Monitor::new(good, Limits::default()).unwrap();

    // gap
    let e = m.apply_step(&step(1, "t", serde_json::json!({}), false)).unwrap_err();
    assert_eq!(e.kind, ErrorKind::StateConflict);
    assert_eq!(e.reason, "step_index_gap");
    log_event("err-state", "state", "step_index_gap", e.detail.clone());

    m.apply_step(&step(0, "t", serde_json::json!({}), false)).unwrap();
    // duplicate / reorder
    let e = m.apply_step(&step(0, "t", serde_json::json!({}), false)).unwrap_err();
    assert_eq!(e.kind, ErrorKind::StateConflict);
    assert_eq!(e.reason, "step_index_duplicate_or_reordered");

    // close, then refuse more
    m.apply_step(&Step {
        index: 1,
        event: Default::default(),
        ruleset_version: None,
        end: true,
    })
    .unwrap();
    let e = m.apply_step(&step(2, "t", serde_json::json!({}), false)).unwrap_err();
    assert_eq!(e.kind, ErrorKind::StateConflict);
    assert_eq!(e.reason, "monitor_closed");
    log_event("err-state", "state", "monitor_closed", e.detail.clone());

    // step pinned to a different ruleset version
    let good2 = rs_with(RuleDef::Response(ResponseRuleDef {
        id: "r".to_string(),
        trigger: EventPattern { event_type: "t".to_string(), where_: None },
        response: EventPattern { event_type: "r".to_string(), where_: None },
        within_steps: 2,
        consume: ConsumePolicy::AllMatching,
        correlation_key: None,
    }));
    let mut m2 = Monitor::new(good2, Limits::default()).unwrap();
    let mut pinned = step(0, "t", serde_json::json!({}), false);
    pinned.ruleset_version = Some("9.9.9".to_string());
    let e = m2.apply_step(&pinned).unwrap_err();
    assert_eq!(e.kind, ErrorKind::StateConflict);
    assert_eq!(e.reason, "ruleset_version_mismatch");
}

#[test]
fn resource_exhaustion_is_distinct_from_other_failures() {
    let good = rs_with(RuleDef::Response(ResponseRuleDef {
        id: "r".to_string(),
        trigger: EventPattern { event_type: "t".to_string(), where_: None },
        response: EventPattern { event_type: "r".to_string(), where_: None },
        within_steps: 100,
        consume: ConsumePolicy::AllMatching,
        correlation_key: None,
    }));
    let limits = Limits {
        max_steps: 3,
        max_obligations_total: 5,
        max_obligations_per_step: 10,
        max_monitors: 1,
    };
    let mut m = Monitor::new(good, limits).unwrap();
    m.apply_step(&step(0, "t", serde_json::json!({}), false)).unwrap();
    m.apply_step(&step(1, "x", serde_json::json!({}), false)).unwrap();
    m.apply_step(&step(2, "x", serde_json::json!({}), false)).unwrap();
    let e = m.apply_step(&step(3, "x", serde_json::json!({}), false)).unwrap_err();
    assert_eq!(e.kind, ErrorKind::ResourceExhausted);
    assert_eq!(e.reason, "max_steps_exceeded");
    log_event("err-resource", "resource", "max_steps_exceeded", e.detail.clone());

    // Total obligation cap: 6 triggers with cap 5.
    let good = rs_with(RuleDef::Response(ResponseRuleDef {
        id: "r".to_string(),
        trigger: EventPattern { event_type: "t".to_string(), where_: None },
        response: EventPattern { event_type: "r".to_string(), where_: None },
        within_steps: 100,
        consume: ConsumePolicy::AllMatching,
        correlation_key: None,
    }));
    let limits = Limits {
        max_steps: 100,
        max_obligations_total: 5,
        max_obligations_per_step: 10,
        max_monitors: 1,
    };
    let mut m = Monitor::new(good, limits).unwrap();
    for i in 0..5u64 {
        m.apply_step(&step(i, "t", serde_json::json!({}), false)).unwrap();
    }
    let e = m.apply_step(&step(5, "t", serde_json::json!({}), false)).unwrap_err();
    assert_eq!(e.kind, ErrorKind::ResourceExhausted);
    assert_eq!(e.reason, "max_obligations_total_exceeded");

    // per-step cap
    let two = Ruleset {
        id: "err".to_string(),
        version: "1".to_string(),
        rules: vec![
            RuleDef::Response(ResponseRuleDef {
                id: "a".to_string(),
                trigger: EventPattern { event_type: "t".to_string(), where_: None },
                response: EventPattern { event_type: "r".to_string(), where_: None },
                within_steps: 100,
                consume: ConsumePolicy::AllMatching,
                correlation_key: None,
            }),
            RuleDef::Response(ResponseRuleDef {
                id: "b".to_string(),
                trigger: EventPattern { event_type: "t".to_string(), where_: None },
                response: EventPattern { event_type: "r".to_string(), where_: None },
                within_steps: 100,
                consume: ConsumePolicy::AllMatching,
                correlation_key: None,
            }),
        ],
    };
    let limits = Limits {
        max_steps: 100,
        max_obligations_total: 100,
        max_obligations_per_step: 1,
        max_monitors: 1,
    };
    let mut m = Monitor::new(two, limits).unwrap();
    let e = m.apply_step(&step(0, "t", serde_json::json!({}), false)).unwrap_err();
    assert_eq!(e.kind, ErrorKind::ResourceExhausted);
    assert_eq!(e.reason, "max_obligations_per_step_exceeded");
}

#[test]
fn computation_failure_on_badly_typed_fact() {
    // gt against a string fact is a computation failure (not silently false).
    let rs = rs_with(RuleDef::Sustain(SustainRuleDef {
        id: "s".to_string(),
        condition: Predicate::Gt { path: "amount".to_string(), value: serde_json::json!(10) },
        duration_steps: 1,
        scope: SustainScope::Always,
    }));
    let mut m = Monitor::new(rs, Limits::default()).unwrap();
    let e = m
        .apply_step(&step(0, "anything", serde_json::json!({"amount": "not-a-number"}), false))
        .unwrap_err();
    assert_eq!(e.kind, ErrorKind::ComputationFailed);
    assert_eq!(e.reason, "non_numeric_fact");
    log_event("err-compute", "compute", "non_numeric_fact", e.detail.clone());

    // Bool predicate on a non-boolean fact.
    let rs2 = rs_with(RuleDef::Sustain(SustainRuleDef {
        id: "s".to_string(),
        condition: Predicate::Bool { path: "flag".to_string() },
        duration_steps: 1,
        scope: SustainScope::Always,
    }));
    let mut m2 = Monitor::new(rs2, Limits::default()).unwrap();
    let e = m2
        .apply_step(&step(0, "x", serde_json::json!({"flag": "yes"}), false))
        .unwrap_err();
    assert_eq!(e.kind, ErrorKind::ComputationFailed);
    assert_eq!(e.reason, "non_boolean_fact");

    // Transactional guarantee: the failed step did not mutate the monitor.
    assert_eq!(m2.step_count(), 0);
    assert!(!m2.is_closed());
    // A valid following step applies at index 0.
    let ok = m2.apply_step(&step(0, "x", serde_json::json!({"flag": true}), false)).unwrap();
    assert_eq!(ok.index, 0);
}
