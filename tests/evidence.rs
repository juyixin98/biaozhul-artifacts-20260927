//! Evidence and versioning tests:
//! - snapshot digest verifies; restore yields an observationally equal monitor
//! - restoring then continuing gives identical verdicts to an uninterrupted run
//! - tampering with snapshot content or a single journal entry is detected
//! - wrong-version restore is a state conflict; rotating to the same version
//!   is an input error; obligations from different epochs never mix

mod common;

use common::*;

use btmon::canonical::{canonical_bytes, canonical_digest, sha256_hex};
use btmon::error::Category;
use btmon::lang::*;
use btmon::monitor::{Limits, Monitor};
use serde_json::{json, Value};

fn resp_ruleset(version: &str, close: ClosePolicy) -> RuleSet {
    RuleSet {
        version: version.into(),
        response: vec![ResponseRule {
            id: "pay".into(),
            trigger: Condition {
                all: vec![Atom {
                    field: "kind".into(),
                    op: Op::Eq,
                    value: json!("order"),
                }],
            },
            response: Atom {
                field: "kind".into(),
                op: Op::Eq,
                value: json!("payment"),
            },
            after: 0,
            within: 3,
            satisfy: SatisfyMode::All,
            on_close: close,
        }],
        sustain: vec![],
    }
}

#[test]
fn snapshot_roundtrip_preserves_state() {
    let rs = resp_ruleset("v1", ClosePolicy::Strict);
    let mut m = Monitor::new("m".into(), "ev-1".into(), rs, Limits::default()).unwrap();
    m.append(&Event::new("order"), None).unwrap();
    m.append(&Event::new("other"), None).unwrap();

    let snap = m.snapshot();
    assert!(snap.get("digest").and_then(|v| v.as_str()).is_some());
    m.verify_chain().unwrap();

    let restored = Monitor::restore(snap.clone(), None).unwrap();
    assert_eq!(restored.next_step, 2);
    assert_eq!(restored.global_verdict(), Verdict::Wait);
    assert_eq!(restored.obligations.len(), 1);

    // Continue both in lockstep: verdicts and obligation states must match.
    let mut cont = restored;
    cont.append(&Event::new("payment"), None).unwrap();
    m.append(&Event::new("payment"), None).unwrap();
    assert_eq!(cont.global_verdict(), m.global_verdict());
    assert_eq!(cont.obligations[0].status, ObligationStatus::Satisfied);
    assert_eq!(cont.obligations[0].satisfied_at, Some(2));
}

#[test]
fn restore_after_close_is_stable() {
    let f = load_run("01-overlap-all");
    let mut d = drive(&f);
    d.monitor.end().unwrap();
    let snap = d.monitor.snapshot();
    let restored = Monitor::restore(snap, None).unwrap();
    assert!(restored.closed);
    assert_eq!(restored.global_verdict(), Verdict::Sat);
    // A closed monitor rejects further events.
    let e = restored_monitor_append_closed(&restored);
    assert_eq!(e.category, Category::State);
    assert_eq!(e.code, "MONITOR_CLOSED");
}

fn restored_monitor_append_closed(m: &Monitor) -> btmon::error::KernelError {
    // Clone is fine here through a snapshot; we only need the error type.
    let snap = m.snapshot();
    let mut again = Monitor::restore(snap, None).unwrap();
    again.append(&Event::new("x"), None).unwrap_err()
}

#[test]
fn tampered_snapshot_content_is_rejected() {
    let rs = resp_ruleset("v1", ClosePolicy::Strict);
    let mut m = Monitor::new("m".into(), "ev-2".into(), rs, Limits::default()).unwrap();
    m.append(&Event::new("order"), None).unwrap();
    let mut snap = m.snapshot();
    // Flip an obligation status without updating the digest.
    snap["obligations"][0]["status"] = json!("satisfied");
    let err = Monitor::restore(snap, None).unwrap_err();
    assert_eq!(err.category, Category::State);
    assert_eq!(err.code, "SNAPSHOT_DIGEST_MISMATCH");
}

#[test]
fn tampered_snapshot_with_recomputed_digest_still_fails_chain() {
    let rs = resp_ruleset("v1", ClosePolicy::Strict);
    let mut m = Monitor::new("m".into(), "ev-3".into(), rs, Limits::default()).unwrap();
    m.append(&Event::new("order"), None).unwrap();
    m.append(&Event::new("other"), None).unwrap();
    let mut snap = m.snapshot();
    // Tamper with an old journal entry, then forge the snapshot digest.
    snap["decisions"][0]["type"] = json!("forged");
    let forged_digest = canonical_digest(&snap);
    snap["digest"] = json!(forged_digest);
    let err = Monitor::restore(snap, None).unwrap_err();
    assert_eq!(err.category, Category::State);
    assert_eq!(err.code, "DECISION_CHAIN_BROKEN");
}

#[test]
fn chain_is_hash_linked() {
    let rs = resp_ruleset("v1", ClosePolicy::Strict);
    let mut m = Monitor::new("m".into(), "ev-4".into(), rs, Limits::default()).unwrap();
    m.append(&Event::new("order"), None).unwrap();
    // First entry prev_hash is the genesis sentinel; each subsequent hash
    // chains from the previous one.
    assert!(m.decisions.len() >= 2);
    assert_eq!(m.decisions[0].prev_hash, "GENESIS");
    for w in m.decisions.windows(2) {
        assert_eq!(w[1].prev_hash, w[0].hash);
    }
    // Every stored hash really is sha256 over the canonical entry minus hash.
    for e in &m.decisions {
        let mut v = serde_json::to_value(e).unwrap();
        v.as_object_mut().unwrap().remove("hash");
        assert_eq!(sha256_hex(&canonical_bytes(&v)), e.hash);
    }
    // Tampering one entry's detail without touching hashes breaks verify.
    // Simulate on a serialised snapshot path for realism.
    let mut snap = m.snapshot();
    snap["decisions"][1]["detail"]["event"]["kind"] = json!("TAMPERED");
    let digest = canonical_digest(&snap);
    snap["digest"] = json!(digest);
    let err = Monitor::restore(snap, None).unwrap_err();
    assert_eq!(err.code, "DECISION_CHAIN_BROKEN");
}

#[test]
fn wrong_version_restore_is_state_conflict() {
    let rs = resp_ruleset("v1", ClosePolicy::Strict);
    let mut m = Monitor::new("m".into(), "ev-5".into(), rs, Limits::default()).unwrap();
    m.append(&Event::new("order"), None).unwrap();
    let snap = m.snapshot();
    let err = Monitor::restore(snap, Some("v2")).unwrap_err();
    assert_eq!(err.category, Category::State);
    assert_eq!(err.code, "VERSION_MISMATCH");
}

#[test]
fn rotation_cannot_reuse_version_and_seals_old_state() {
    let rs1 = resp_ruleset("v1", ClosePolicy::Strict);
    let mut m = Monitor::new("m".into(), "ev-6".into(), rs1.clone(), Limits::default()).unwrap();
    m.append(&Event::new("order"), None).unwrap(); // pending 0:pay:0 [0,2]
    let err = m
        .rotate(resp_ruleset("v1", ClosePolicy::Strict))
        .unwrap_err();
    assert_eq!(err.category, Category::Input);
    assert_eq!(err.code, "SAME_VERSION");

    m.rotate(resp_ruleset("v2", ClosePolicy::Strict)).unwrap();
    assert_eq!(m.active_epoch, 1);
    // Epoch0 obligation was sealed at the boundary (t0): strict pending.
    let old = &m.obligations[0];
    assert_eq!(old.epoch, 0);
    assert_eq!(old.status, ObligationStatus::Violated);
    assert_eq!(old.reason, ObligationReason::ClosedPending);

    // A v2 payment at t1 cannot satisfy the sealed epoch0 obligation, and
    // without a v2 trigger it spawns nothing.
    let rep = m.append(&Event::new("payment"), Some(1)).unwrap();
    assert!(rep.spawned.is_empty());
    assert!(rep.satisfied.is_empty());
    assert_eq!(m.obligations[0].status, ObligationStatus::Violated);
}

#[test]
fn obligations_carry_epoch_and_versions_never_mix() {
    let rs1 = resp_ruleset("v1", ClosePolicy::Lenient);
    let mut m = Monitor::new("m".into(), "ev-7".into(), rs1, Limits::default()).unwrap();
    m.append(&Event::new("order"), Some(0)).unwrap();
    m.rotate(resp_ruleset("v2", ClosePolicy::Strict)).unwrap();
    m.append(&Event::new("order"), Some(1)).unwrap();
    m.end().unwrap();
    let epochs: Vec<usize> = m.obligations.iter().map(|o| o.epoch).collect();
    assert_eq!(epochs, vec![0, 1]);
    // Same rule id, different epochs => distinct obligations.
    let ids: Vec<&str> = m.obligations.iter().map(|o| o.id.as_str()).collect();
    assert_eq!(ids, vec!["0:pay:0", "1:pay:1"]);
    let rules = m.rule_outcomes();
    let versions: Vec<&str> = rules.iter().map(|r| r.version.as_str()).collect();
    assert!(versions.contains(&"v1"));
    assert!(versions.contains(&"v2"));
}

#[test]
fn snapshot_digest_changes_when_state_changes() {
    let rs = resp_ruleset("v1", ClosePolicy::Strict);
    let mut m = Monitor::new("m".into(), "ev-8".into(), rs, Limits::default()).unwrap();
    let d0 = m.snapshot()["digest"].as_str().unwrap().to_string();
    m.append(&Event::new("order"), None).unwrap();
    let d1 = m.snapshot()["digest"].as_str().unwrap().to_string();
    assert_ne!(d0, d1);
    let _: Value = json!({});
}

#[test]
fn end_with_zero_events_is_vacuous_sat() {
    let rs1 = resp_ruleset("v1", ClosePolicy::Strict);
    let mut m = Monitor::new("z".into(), "ev-z1".into(), rs1, Limits::default()).unwrap();
    let r = m.end().unwrap();
    assert_eq!(r.sealed, 0);
    assert_eq!(r.verdict, Verdict::Sat);
    assert!(m.verify_chain().is_ok());
}

#[test]
fn rotate_before_any_event_then_track_new_epoch() {
    let rs1 = resp_ruleset("v1", ClosePolicy::Strict);
    let mut m = Monitor::new("z".into(), "ev-z2".into(), rs1, Limits::default()).unwrap();
    // No events yet; boundary step is -1; must not crash and seals nothing.
    m.rotate(resp_ruleset("v2", ClosePolicy::Strict)).unwrap();
    assert_eq!(m.active_epoch, 1);
    assert_eq!(m.next_step, 0);
    m.append(&Event::new("order"), Some(0)).unwrap();
    assert_eq!(m.obligations[0].id, "1:pay:0");
    m.end().unwrap();
    assert_eq!(m.obligations[0].status, ObligationStatus::Violated);
    assert_eq!(m.obligations[0].reason, ObligationReason::ClosedPending);
}
