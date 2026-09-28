//! Error-contract tests: the four distinguishable failure categories plus
//! not-found. Each test asserts the stable `code`, the `Category` and (in
//! the HTTP suite) the status code — never just "the call failed".

mod common;

use btmon::error::Category;
use btmon::lang::*;
use btmon::monitor::{Limits, Monitor};
use serde_json::json;

fn trigger_order() -> Condition {
    Condition {
        all: vec![Atom {
            field: "kind".into(),
            op: Op::Eq,
            value: json!("order"),
        }],
    }
}
fn response_payment() -> Atom {
    Atom {
        field: "kind".into(),
        op: Op::Eq,
        value: json!("payment"),
    }
}

fn rr(within: i64, close: ClosePolicy) -> RuleSet {
    RuleSet {
        version: "v1".into(),
        response: vec![ResponseRule {
            id: "pay".into(),
            trigger: trigger_order(),
            response: response_payment(),
            after: 0,
            within,
            satisfy: SatisfyMode::All,
            on_close: close,
        }],
        sustain: vec![],
    }
}

// -------------------------------------------------------------- input (400)
#[test]
fn empty_ruleset_is_input_error() {
    let e = Monitor::new(
        "m".into(),
        "e".into(),
        RuleSet {
            version: "v1".into(),
            response: vec![],
            sustain: vec![],
        },
        Limits::default(),
    )
    .unwrap_err();
    assert_eq!(e.category, Category::Input);
    assert_eq!(e.code, "EMPTY_RULESET");
    assert_eq!(e.http_status(), 400);
}

#[test]
fn bad_window_is_input_error() {
    let e = Monitor::new(
        "m".into(),
        "e".into(),
        rr(0, ClosePolicy::Strict),
        Limits::default(),
    )
    .unwrap_err();
    assert_eq!(e.code, "BAD_WINDOW");
    assert_eq!(e.category, Category::Input);

    let mut rs = rr(1, ClosePolicy::Strict);
    rs.response[0].after = -1;
    let e = Monitor::new("m".into(), "e".into(), rs, Limits::default()).unwrap_err();
    assert_eq!(e.code, "BAD_WINDOW");
}

#[test]
fn duplicate_rule_id_is_input_error() {
    let mut rs = rr(1, ClosePolicy::Strict);
    rs.response.push(rs.response[0].clone());
    let e = Monitor::new("m".into(), "e".into(), rs, Limits::default()).unwrap_err();
    assert_eq!(e.code, "DUPLICATE_RULE_ID");
    assert_eq!(e.category, Category::Input);
}

#[test]
fn empty_event_is_input_error() {
    let mut m = Monitor::new(
        "m".into(),
        "e".into(),
        rr(2, ClosePolicy::Strict),
        Limits::default(),
    )
    .unwrap();
    let e = m
        .append(
            &Event {
                attrs: serde_json::Map::new(),
            },
            None,
        )
        .unwrap_err();
    assert_eq!(e.code, "EMPTY_EVENT");
    assert_eq!(e.category, Category::Input);
}

#[test]
fn step_gap_is_input_error() {
    let mut m = Monitor::new(
        "m".into(),
        "e".into(),
        rr(2, ClosePolicy::Strict),
        Limits::default(),
    )
    .unwrap();
    m.append(&Event::new("order"), Some(0)).unwrap();
    let e = m.append(&Event::new("x"), Some(2)).unwrap_err();
    assert_eq!(e.code, "STEP_GAP");
    assert_eq!(e.category, Category::Input);
    assert_eq!(e.http_status(), 400);
    // Failed append must not advance the step counter.
    assert_eq!(m.next_step, 1);
}

#[test]
fn invalid_sustain_window_input_error() {
    let rs = RuleSet {
        version: "v1".into(),
        response: vec![],
        sustain: vec![SustainRule {
            id: "s".into(),
            trigger: trigger_order(),
            sustain: trigger_order(),
            after: 0,
            duration: 0,
            on_close: ClosePolicy::Strict,
        }],
    };
    let e = Monitor::new("m".into(), "e".into(), rs, Limits::default()).unwrap_err();
    assert_eq!(e.code, "BAD_WINDOW");
}

// -------------------------------------------------------------- state (409)
#[test]
fn append_after_close_is_state_conflict() {
    let mut m = Monitor::new(
        "m".into(),
        "e".into(),
        rr(2, ClosePolicy::Strict),
        Limits::default(),
    )
    .unwrap();
    m.append(&Event::new("order"), None).unwrap();
    m.end().unwrap();
    let e = m.append(&Event::new("payment"), None).unwrap_err();
    assert_eq!(e.code, "MONITOR_CLOSED");
    assert_eq!(e.category, Category::State);
    assert_eq!(e.http_status(), 409);

    let e2 = m.end().unwrap_err();
    assert_eq!(e2.code, "MONITOR_CLOSED");
}

#[test]
fn step_regression_is_state_conflict() {
    let mut m = Monitor::new(
        "m".into(),
        "e".into(),
        rr(5, ClosePolicy::Strict),
        Limits::default(),
    )
    .unwrap();
    m.append(&Event::new("order"), Some(0)).unwrap();
    m.append(&Event::new("x"), Some(1)).unwrap();
    let e = m.append(&Event::new("x"), Some(0)).unwrap_err();
    assert_eq!(e.code, "STEP_REGRESSED");
    assert_eq!(e.category, Category::State);
    assert_eq!(e.http_status(), 409);
}

// ----------------------------------------------------------- resource (507)
#[test]
fn obligation_limit_is_resource_error() {
    let limits = Limits {
        max_active_obligations: 3,
        max_epochs: 4,
        max_log_bytes: 1 << 20,
    };
    let mut m = Monitor::new("m".into(), "e".into(), rr(100, ClosePolicy::Strict), limits).unwrap();
    // Each trigger spawns a long-lived pending obligation; the 4th must fail.
    m.append(&Event::new("order"), Some(0)).unwrap();
    m.append(&Event::new("order"), Some(1)).unwrap();
    m.append(&Event::new("order"), Some(2)).unwrap();
    let before = m.obligations.len();
    let e = m.append(&Event::new("order"), Some(3)).unwrap_err();
    assert_eq!(e.code, "OBLIGATION_LIMIT");
    assert_eq!(e.category, Category::Resource);
    assert_eq!(e.http_status(), 507);
    // Rejected step left state untouched.
    assert_eq!(m.obligations.len(), before);
    assert_eq!(m.next_step, 3);
}

#[test]
fn epoch_limit_is_resource_error() {
    let limits = Limits {
        max_active_obligations: 64,
        max_epochs: 2,
        max_log_bytes: 1 << 20,
    };
    let mut m = Monitor::new("m".into(), "e".into(), rr(2, ClosePolicy::Strict), limits).unwrap();
    let mut v = 2;
    m.rotate(rr_version(&format!("v{v}"), 2, ClosePolicy::Strict))
        .unwrap();
    v += 1;
    // active_epoch now 1; max_epochs=2 means a second rotate must fail.
    let e = m
        .rotate(rr_version(&format!("v{v}"), 2, ClosePolicy::Strict))
        .unwrap_err();
    assert_eq!(e.code, "EPOCH_LIMIT");
    assert_eq!(e.category, Category::Resource);
    assert_eq!(e.http_status(), 507);
}

#[test]
fn log_budget_is_resource_error() {
    let limits = Limits {
        max_active_obligations: 4096,
        max_epochs: 64,
        max_log_bytes: 200,
    };
    let mut m = Monitor::new("m".into(), "e".into(), rr(100, ClosePolicy::Strict), limits).unwrap();
    // Each apppend emits several chained entries; a 200-byte budget must be
    // exhausted quickly and the failure must leave the step uncommitted.
    let mut hit = false;
    for s in 0..10i64 {
        if let Err(e) = m.append(&Event::new("x").with("n", s), Some(s)) {
            assert_eq!(e.code, "DECISION_LOG_LIMIT");
            assert_eq!(e.category, Category::Resource);
            assert_eq!(e.http_status(), 507);
            hit = true;
            break;
        }
    }
    assert!(hit, "tiny log budget must be exhausted");
}

// -------------------------------------------------------- computation (500)
#[test]
fn forged_next_seq_is_computation_error() {
    let mut m = Monitor::new(
        "m".into(),
        "e".into(),
        rr(2, ClosePolicy::Strict),
        Limits::default(),
    )
    .unwrap();
    m.append(&Event::new("order"), None).unwrap();
    let mut snap = m.snapshot();
    // A structurally valid snapshot whose internal counters disagree is an
    // internal inconsistency, not the client's fault.
    snap["next_seq"] = json!(999);
    let digest = btmon::canonical::canonical_digest(&snap);
    snap["digest"] = json!(digest);
    let e = Monitor::restore(snap, None).unwrap_err();
    assert_eq!(e.category, Category::Computation);
    assert_eq!(e.code, "COMPUTATION_FAILED");
    assert_eq!(e.http_status(), 500);
}

fn rr_version(version: &str, within: i64, close: ClosePolicy) -> RuleSet {
    let mut rs = rr(within, close);
    rs.version = version.into();
    rs
}
