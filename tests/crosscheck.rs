//! Property-style cross-validation: build many small random traces over both
//! rule kinds and both satisfy modes, then compare the incremental kernel
//! against the independent set-based oracle field-for-field. The trace
//! generator is a tiny deterministic LCG seeded per test (no external crate).

mod common;

use common::*;

use btmon::lang::*;
use btmon::monitor::{Limits, Monitor};
use btmon::reference;

struct Lcg(u64);
impl Lcg {
    fn next_u64(&mut self) -> u64 {
        // Numerical Recipes constants.
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0
    }
    fn below(&mut self, n: u64) -> usize {
        (self.next_u64() % n) as usize
    }
}

fn kind_atom(k: &str) -> Condition {
    Condition {
        all: vec![Atom {
            field: "kind".into(),
            op: Op::Eq,
            value: serde_json::json!(k),
        }],
    }
}

fn event(k: &str) -> Event {
    Event::new(k)
}

fn ruleset(version: &str, satisfy: SatisfyMode, close: ClosePolicy) -> RuleSet {
    RuleSet {
        version: version.into(),
        response: vec![ResponseRule {
            id: "r".into(),
            trigger: kind_atom("t"),
            response: Atom {
                field: "kind".into(),
                op: Op::Eq,
                value: serde_json::json!("a"),
            },
            after: 0,
            within: 3,
            satisfy,
            on_close: close,
        }],
        sustain: vec![SustainRule {
            id: "s".into(),
            trigger: kind_atom("u"),
            sustain: kind_atom("g"),
            after: 0,
            duration: 2,
            on_close: close,
        }],
    }
}

fn run_case(seed: u64, satisfy: SatisfyMode, close_policy: ClosePolicy, close: bool) {
    let mut rng = Lcg(seed);
    let rs = ruleset("v1", satisfy, close_policy);
    let alphabet = ["t", "a", "u", "g", "x"];
    let len = 1 + rng.below(12);
    let trace: Vec<(i64, Event)> = (0..len)
        .map(|i| {
            let k = alphabet[rng.below(alphabet.len() as u64)];
            (i as i64, event(k))
        })
        .collect();

    let mut monitor = Monitor::new(
        "prop".into(),
        format!("prop-{seed}"),
        rs.clone(),
        Limits::replay(),
    )
    .unwrap();
    for (_, ev) in &trace {
        monitor.append(ev, None).unwrap();
    }
    let open_oracle = reference::evaluate(&trace, 0, 0, &rs, false);
    assert_kernel_matches_oracle(&monitor, &open_oracle);

    if close {
        monitor.end().unwrap();
        let closed_oracle = reference::evaluate(&trace, 0, 0, &rs, true);
        assert_kernel_matches_oracle(&monitor, &closed_oracle);
        // Global verdicts must agree too.
        let ov = reference::global_verdict(&closed_oracle);
        assert_eq!(monitor.global_verdict(), ov, "seed {seed}: global verdict");
    } else {
        let ov = reference::global_verdict(&open_oracle);
        assert_eq!(
            monitor.global_verdict(),
            ov,
            "seed {seed}: open global verdict"
        );
    }
}

#[test]
fn random_traces_all_modes_open() {
    for seed in 1..=200u64 {
        run_case(seed, SatisfyMode::All, ClosePolicy::Strict, false);
    }
}

#[test]
fn random_traces_one_modes_open() {
    for seed in 1000..=1200u64 {
        run_case(seed, SatisfyMode::One, ClosePolicy::Strict, false);
    }
}

#[test]
fn random_traces_all_modes_closed_strict() {
    for seed in 2000..=2200u64 {
        run_case(seed, SatisfyMode::All, ClosePolicy::Strict, true);
    }
}

#[test]
fn random_traces_one_modes_closed_lenient() {
    for seed in 3000..=3200u64 {
        run_case(seed, SatisfyMode::One, ClosePolicy::Lenient, true);
    }
}

/// Rotation on random cut points: segments must agree with the oracle's
/// segment semantics, and old obligations must never observe new events.
#[test]
fn random_traces_with_rotation() {
    let mut rng = Lcg(0xC0FFEE);
    for seed in 1..=80u64 {
        let rs1 = ruleset("v1", SatisfyMode::All, ClosePolicy::Strict);
        let mut rs2 = ruleset("v2", SatisfyMode::One, ClosePolicy::Lenient);
        rs2.response[0].within = 2;
        let len = 4 + rng.below(8) as i64;
        let cut = 1 + rng.below((len - 1) as u64) as i64;
        let alphabet = ["t", "a", "u", "g", "x"];
        let full: Vec<Event> = (0..len)
            .map(|_| alphabet[rng.below(alphabet.len() as u64)])
            .map(event)
            .collect();

        let mut monitor = Monitor::new(
            "rot".into(),
            format!("rot-{seed}"),
            rs1.clone(),
            Limits::replay(),
        )
        .unwrap();
        let mut seg1: Vec<(i64, Event)> = Vec::new();
        let mut seg2: Vec<(i64, Event)> = Vec::new();
        for (i, ev) in full.iter().enumerate() {
            let i = i as i64;
            if i == cut {
                monitor.rotate(rs2.clone()).unwrap();
            }
            monitor.append(ev, Some(i)).unwrap();
            if i < cut {
                seg1.push((i, ev.clone()));
            } else {
                seg2.push((i, ev.clone()));
            }
        }
        monitor.end().unwrap();
        let oracle = reference::evaluate_run(&[(rs1.clone(), seg1), (rs2.clone(), seg2)], true);
        assert_kernel_matches_oracle(&monitor, &oracle);
    }
}
