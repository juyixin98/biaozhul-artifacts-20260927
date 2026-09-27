//! Kernel-level semantic tests, including cases too fiddly for the JSON
//! fixtures.  Every test logs its intermediate states under a distinct run
//! id.

mod common;

use bounded_monitor::error::ErrorKind;
use bounded_monitor::kernel::{Limits, Monitor, ObligationStatus, Verdict};
use bounded_monitor::language::{
    ConsumePolicy, EventPattern, Predicate, ResponseRuleDef, RuleDef, Ruleset, Step, SustainRuleDef,
    SustainScope,
};
use common::{log_event, log_state, step};

fn response_rule(id: &str, within: u64, consume: ConsumePolicy) -> RuleDef {
    RuleDef::Response(ResponseRuleDef {
        id: id.to_string(),
        trigger: EventPattern { event_type: "t".to_string(), where_: None },
        response: EventPattern { event_type: "r".to_string(), where_: None },
        within_steps: within,
        consume,
        correlation_key: None,
    })
}

fn ruleset(version: &str, rules: Vec<RuleDef>) -> Ruleset {
    Ruleset { id: "unit".to_string(), version: version.to_string(), rules }
}

fn end_step(index: u64) -> Step {
    Step {
        index,
        event: bounded_monitor::language::Event::default(),
        ruleset_version: None,
        end: true,
    }
}

#[test]
fn response_exactly_on_deadline_is_satisfied() {
    let rid = "unit-boundary-exact";
    // within_steps=2: trigger at 0, deadline 2; response at exactly 2.
    let rs = ruleset("1", vec![response_rule("rr", 2, ConsumePolicy::AllMatching)]);
    let mut m = Monitor::new(rs, Limits::default()).unwrap();

    let o0 = m.apply_step(&step(0, "t", serde_json::json!({}), false)).unwrap();
    log_state(rid, "boundary_exact", "trigger", &o0, &m.obligations(), "trigger at 0, deadline 2");
    assert_eq!(m.verdict(), Verdict::Pending, "pending must not read as satisfied");

    let o1 = m.apply_step(&step(1, "x", serde_json::json!({}), false)).unwrap();
    log_state(rid, "boundary_exact", "gap", &o1, &m.obligations(), "unrelated event at 1");
    assert_eq!(m.verdict(), Verdict::Pending);

    let o2 = m.apply_step(&step(2, "r", serde_json::json!({}), false)).unwrap();
    log_state(rid, "boundary_exact", "boundary_response", &o2, &m.obligations(), "response at deadline 2");
    assert_eq!(m.verdict(), Verdict::Pending, "still pending until the trace is closed");
    assert_eq!(o2.resolved, vec!["rr#o0"]);

    let o3 = m.apply_step(&end_step(3)).unwrap();
    log_state(rid, "boundary_exact", "close", &o3, &m.obligations(), "end marker closes trace");
    assert_eq!(o3.verdict, Verdict::Satisfied);
    let obl = &m.obligations()[0];
    assert_eq!(obl.status, ObligationStatus::Satisfied);
    assert_eq!(obl.resolution_step, Some(2));
    assert_eq!(obl.violation_step, None);
}

#[test]
fn response_one_step_past_deadline_is_violated() {
    let rid = "unit-boundary-late";
    let rs = ruleset("1", vec![response_rule("rr", 2, ConsumePolicy::AllMatching)]);
    let mut m = Monitor::new(rs, Limits::default()).unwrap();

    m.apply_step(&step(0, "t", serde_json::json!({}), false)).unwrap();
    m.apply_step(&step(1, "x", serde_json::json!({}), false)).unwrap();
    // step 2 with a non-response event: obligation must already be violated.
    let outcome2 = m.apply_step(&step(2, "x", serde_json::json!({}), false)).unwrap();
    log_state(rid, "boundary_late", "deadline_no_response", &outcome2, &m.obligations(), "deadline 2 expired");
    assert_eq!(outcome2.violated, vec!["rr#o0"]);
    assert_eq!(m.verdict(), Verdict::Violated, "violation is final even before close");

    // A later response cannot repair the violated obligation.
    let late = m.apply_step(&step(3, "r", serde_json::json!({}), false)).unwrap();
    assert!(late.resolved.is_empty(), "late response must not repair a violation");
    let obl = &m.obligations()[0];
    assert_eq!(obl.status, ObligationStatus::Violated);
    assert_eq!(obl.violation_step, Some(2));
}

#[test]
fn two_open_sustain_windows_break_at_one_step() {
    let rid = "unit-sustain-both-break";
    // fan_on triggers hold(fan_on OR cool) for 3 steps.  Triggers at 0 and
    // 1 leave BOTH windows open at step 2: [0,2] and [1,3].  cool=false at
    // step 2 breaks both simultaneously (window #0 breaks at its deadline).
    let cool = RuleDef::Sustain(SustainRuleDef {
        id: "cool".to_string(),
        condition: Predicate::Or {
            any: vec![
                Predicate::Eq { path: "type".to_string(), value: serde_json::json!("fan_on") },
                Predicate::Eq { path: "cool".to_string(), value: serde_json::json!(true) },
            ],
        },
        duration_steps: 3,
        scope: SustainScope::AfterTrigger {
            trigger: EventPattern { event_type: "fan_on".to_string(), where_: None },
        },
    });
    let rs = ruleset("1", vec![cool]);
    let mut m = Monitor::new(rs, Limits::default()).unwrap();

    let o0 = m.apply_step(&step(0, "fan_on", serde_json::json!({"cool": true}), false)).unwrap();
    assert_eq!(o0.spawned, vec!["cool#o0"]);
    let o1 = m.apply_step(&step(1, "fan_on", serde_json::json!({"cool": true}), false)).unwrap();
    assert_eq!(o1.spawned, vec!["cool#o1"]);
    let o2 = m
        .apply_step(&step(2, "tick", serde_json::json!({"cool": false}), false))
        .unwrap();
    log_state(
        rid,
        "sustain_both_break",
        "double_break",
        &o2,
        &m.obligations(),
        "condition false at s2 breaks cool#o0 (deadline) and cool#o1",
    );
    let mut violated: Vec<_> = o2.violated.to_vec();
    violated.sort();
    assert_eq!(violated, vec!["cool#o0", "cool#o1"]);
    assert_eq!(m.verdict(), Verdict::Violated);
    for obl in m.obligations() {
        assert_eq!(obl.status, ObligationStatus::Violated);
        assert_eq!(obl.violation_step, Some(2));
    }
}

#[test]
fn earliest_deadline_consume_resolves_one_obligation() {
    let rid = "unit-consume-earliest";
    let rs = ruleset("1", vec![response_rule("rr", 3, ConsumePolicy::EarliestDeadline)]);
    let mut m = Monitor::new(rs, Limits::default()).unwrap();

    m.apply_step(&step(0, "t", serde_json::json!({}), false)).unwrap(); // d=3
    m.apply_step(&step(1, "t", serde_json::json!({}), false)).unwrap(); // d=4
    let out = m.apply_step(&step(2, "r", serde_json::json!({}), false)).unwrap();
    log_state(
        rid,
        "consume_earliest",
        "single_consume",
        &out,
        &m.obligations(),
        "one response satisfies only rr#o0 (earliest deadline 3)",
    );
    assert_eq!(out.resolved, vec!["rr#o0"]);
    let pending: Vec<_> = m
        .obligations()
        .into_iter()
        .filter(|o| o.status == ObligationStatus::Pending)
        .map(|o| o.id)
        .collect();
    assert_eq!(pending, vec!["rr#o1"]);
}

#[test]
fn all_matching_consume_resolves_both_overlapping_obligations() {
    let rid = "unit-consume-all";
    let rs = ruleset("1", vec![response_rule("rr", 4, ConsumePolicy::AllMatching)]);
    let mut m = Monitor::new(rs, Limits::default()).unwrap();

    m.apply_step(&step(0, "t", serde_json::json!({}), false)).unwrap();
    m.apply_step(&step(1, "t", serde_json::json!({}), false)).unwrap();
    let out = m.apply_step(&step(2, "r", serde_json::json!({}), false)).unwrap();
    log_state(
        rid,
        "consume_all",
        "multi_consume",
        &out,
        &m.obligations(),
        "one response satisfies BOTH pending obligations (all_matching)",
    );
    let mut resolved = out.resolved.clone();
    resolved.sort();
    assert_eq!(resolved, vec!["rr#o0", "rr#o1"]);
}

#[test]
fn pending_is_never_a_pass_before_close() {
    let rid = "unit-pending-not-pass";
    let rs = ruleset("1", vec![response_rule("rr", 5, ConsumePolicy::AllMatching)]);
    let mut m = Monitor::new(rs, Limits::default()).unwrap();

    m.apply_step(&step(0, "t", serde_json::json!({}), false)).unwrap();
    for n in 1..=4u64 {
        let out = m.apply_step(&step(n, "x", serde_json::json!({}), false)).unwrap();
        assert_eq!(
            out.verdict,
            Verdict::Pending,
            "run {rid}: open trace at step {n} must report pending, never satisfied"
        );
    }
    log_event(rid, "pending_not_pass", "assert", "verdict stayed pending on every open-trace step");
    let close = m.apply_step(&end_step(5)).unwrap();
    assert_eq!(close.verdict, Verdict::Violated, "unresolved at close -> violated, not satisfied");
}

#[test]
fn correlation_requires_equal_key_values() {
    let rid = "unit-correlation";
    let rule = RuleDef::Response(ResponseRuleDef {
        id: "rr".to_string(),
        trigger: EventPattern { event_type: "t".to_string(), where_: None },
        response: EventPattern { event_type: "r".to_string(), where_: None },
        within_steps: 3,
        consume: ConsumePolicy::AllMatching,
        correlation_key: Some("kid".to_string()),
    });
    let rs = ruleset("1", vec![rule]);
    let mut m = Monitor::new(rs, Limits::default()).unwrap();

    m.apply_step(&step(0, "t", serde_json::json!({"kid": "A"}), false)).unwrap();
    // Response with the wrong correlation value matches the event type but
    // must not resolve the obligation.
    let wrong = m.apply_step(&step(1, "r", serde_json::json!({"kid": "B"}), false)).unwrap();
    assert!(wrong.resolved.is_empty());
    log_state(rid, "correlation", "wrong_key", &wrong, &m.obligations(), "response kid=B does not resolve trigger kid=A");
    // Right value at the last possible step resolves it.
    let right = m.apply_step(&step(2, "r", serde_json::json!({"kid": "A"}), false)).unwrap();
    assert_eq!(right.resolved, vec!["rr#o0"]);
    let close = m.apply_step(&end_step(3)).unwrap();
    assert_eq!(close.verdict, Verdict::Satisfied);
}

#[test]
fn restart_with_new_ruleset_version_rejects_old_state() {
    let rid = "unit-version-isolation";
    let rs_v1 = ruleset("1.0.0", vec![response_rule("rr", 2, ConsumePolicy::AllMatching)]);
    let mut m = Monitor::new(rs_v1.clone(), Limits::default()).unwrap();
    m.apply_step(&step(0, "t", serde_json::json!({}), false)).unwrap();
    let snapshot = m.snapshot();

    // Same ruleset id but a new version: restore must be a state conflict.
    let rs_v2 = ruleset("2.0.0", vec![response_rule("rr", 2, ConsumePolicy::AllMatching)]);
    let err = Monitor::restore(snapshot.clone(), &rs_v2).unwrap_err();
    assert_eq!(err.kind, ErrorKind::StateConflict);
    assert_eq!(err.reason, "ruleset_version_mismatch");
    log_event(
        rid,
        "version_isolation",
        "version_conflict",
        "restore under ruleset 2.0.0 rejected: ruleset_version_mismatch",
    );

    // Same version string but changed content: also a conflict, so merely
    // reusing a version number cannot mix old and new rule state.
    let mut tampered = rs_v1.clone();
    tampered.rules[0] = response_rule("rr", 9, ConsumePolicy::AllMatching);
    let err2 = Monitor::restore(snapshot, &tampered).unwrap_err();
    assert_eq!(err2.kind, ErrorKind::StateConflict);
    assert_eq!(err2.reason, "ruleset_content_mismatch");
    log_event(
        rid,
        "version_isolation",
        "content_conflict",
        "same version 1.0.0 with changed within_steps rejected: ruleset_content_mismatch",
    );
}

#[test]
fn recovered_monitor_reaches_same_final_verdict() {
    let rid = "unit-recovery";
    let rs = ruleset("1", vec![response_rule("rr", 3, ConsumePolicy::AllMatching)]);
    let steps = vec![
        step(0, "t", serde_json::json!({}), false),
        step(1, "x", serde_json::json!({}), false),
    ];
    let mut m = Monitor::new(rs.clone(), Limits::default()).unwrap();
    for s in &steps {
        m.apply_step(s).unwrap();
    }
    assert_eq!(m.verdict(), Verdict::Pending);
    let snapshot = m.snapshot();
    log_event(rid, "recovery", "snapshot", "snapshot taken after 2 steps (pending)");

    // Simulate a process restart: rebuild from the snapshot, then feed the
    // suffix.
    let mut recovered = Monitor::restore(snapshot, &rs).unwrap();
    let out = recovered.apply_step(&step(2, "r", serde_json::json!({}), false)).unwrap();
    assert_eq!(out.resolved, vec!["rr#o0"]);
    let close = recovered.apply_step(&end_step(3)).unwrap();
    assert_eq!(close.verdict, Verdict::Satisfied);
    log_state(rid, "recovery", "recovered_run", &close, &recovered.obligations(), "restored run reaches satisfied");

    // A reference process that never restarted must reach the identical end.
    let mut reference = Monitor::new(rs, Limits::default()).unwrap();
    reference.apply_step(&step(0, "t", serde_json::json!({}), false)).unwrap();
    reference.apply_step(&step(1, "x", serde_json::json!({}), false)).unwrap();
    reference.apply_step(&step(2, "r", serde_json::json!({}), false)).unwrap();
    reference.apply_step(&end_step(3)).unwrap();
    assert_eq!(reference.verdict(), recovered.verdict());
    assert_eq!(reference.obligations(), recovered.obligations());
}
