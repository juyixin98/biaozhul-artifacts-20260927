//! 证据验证测试：独立验证器必须确认真反例、否认真“成立”的迹，
//! 并给出一条可逐步核对、端点/动作一致的具体实现路径。

use weak_trace_inclusion::evidence;
use weak_trace_inclusion::input;
use weak_trace_inclusion::model::CheckRequestWire;
use weak_trace_inclusion::solver;

fn parse(v: serde_json::Value) -> weak_trace_inclusion::model::AlignedSystem {
    let req: CheckRequestWire = serde_json::from_value(v).unwrap();
    let (system, _) = input::parse_request(req).unwrap();
    system
}

fn trace_indices(alphabet: &[String], names: &[&str]) -> Vec<u32> {
    names
        .iter()
        .map(|n| {
            alphabet
                .iter()
                .position(|a| a == *n)
                .unwrap_or_else(|| panic!("动作 {n} 不在字母表中")) as u32
        })
        .collect()
}

#[test]
fn verifier_confirms_real_counterexample_and_witness() {
    let v = serde_json::json!({
        "observable_actions": ["a", "b"],
        "specification": {
            "name": "spec",
            "states": ["s0", "s1"],
            "initial_states": ["s0"],
            "edges": [{ "source": "s0", "action": "a", "target": "s1" }]
        },
        "implementation": {
            "name": "impl",
            "states": ["i0", "i1", "i2"],
            "initial_states": ["i0"],
            "hidden_actions": ["tau"],
            "edges": [
                { "id": "tau-loop", "source": "i0", "action": "tau", "target": "i0" },
                { "id": "go-a", "source": "i0", "action": "a", "target": "i1" },
                { "id": "go-b", "source": "i1", "action": "b", "target": "i2" }
            ]
        }
    });
    let system = parse(v);
    let indices = trace_indices(&system.alphabet, &["a", "b"]);

    let report = evidence::verify_trace(
        &system.alphabet,
        &system.specification,
        &system.implementation,
        &indices,
    );
    assert!(report.accepted, "真反例必须通过验证：{report:?}");
    assert_eq!(report.trace, vec!["a", "b"]);
    assert!(report.spec_reachable.is_empty());
    assert_eq!(report.impl_reachable, vec!["i2"]);

    // 具体路径断言：初态 i0，恰好一个观察跳，观察边 id 与 τ 段都可回放。
    let witness = report.impl_witness.as_ref().expect("应给出具体路径");
    assert_eq!(witness.initial_state, "i0");
    assert_eq!(witness.final_state, "i2");
    assert_eq!(witness.hops.len(), 2);
    assert_eq!(witness.hops[0].observable.id.as_deref(), Some("go-a"));
    assert_eq!(witness.hops[1].observable.id.as_deref(), Some("go-b"));
    // 每个检查项都必须通过。
    assert!(report.checks.iter().all(|c| c.passed), "{report:?}");
}

#[test]
fn verifier_rejects_trace_spec_accepts() {
    let v = serde_json::json!({
        "observable_actions": ["a"],
        "specification": {
            "name": "spec",
            "states": ["s0"],
            "initial_states": ["s0"],
            "edges": [{ "source": "s0", "action": "a", "target": "s0" }]
        },
        "implementation": {
            "name": "impl",
            "states": ["i0"],
            "initial_states": ["i0"],
            "edges": [{ "source": "i0", "action": "a", "target": "i0" }]
        }
    });
    let system = parse(v);
    let indices = trace_indices(&system.alphabet, &["a"]);
    let report = evidence::verify_trace(
        &system.alphabet,
        &system.specification,
        &system.implementation,
        &indices,
    );
    assert!(!report.accepted, "规格接受的迹不是反例");
    let reject_check = report
        .checks
        .iter()
        .find(|c| c.name == "spec_rejects_trace")
        .unwrap();
    assert!(!reject_check.passed);
    assert!(reject_check.reason.contains("s0"));
}

#[test]
fn verifier_rejects_trace_impl_cannot_produce() {
    let v = serde_json::json!({
        "observable_actions": ["a", "b"],
        "specification": {
            "name": "spec",
            "states": ["s0"],
            "initial_states": ["s0"],
            "edges": []
        },
        "implementation": {
            "name": "impl",
            "states": ["i0"],
            "initial_states": ["i0"],
            "edges": [{ "source": "i0", "action": "a", "target": "i0" }]
        }
    });
    let system = parse(v);
    // b 两侧都做不到：不是反例（实现不接受）。
    let indices = trace_indices(&system.alphabet, &["b"]);
    let report = evidence::verify_trace(
        &system.alphabet,
        &system.specification,
        &system.implementation,
        &indices,
    );
    assert!(!report.accepted);
    assert!(report.impl_reachable.is_empty());
    assert!(report.impl_witness.is_none());
}

#[test]
fn solver_counterexample_always_corroborated_by_independent_verifier() {
    // 端到端：对一批随机小系统，凡求解器给反例，独立验证器都必须 accepted=true。
    let mut rng = SimpleRng(0x5EED_1234);
    for case in 0..60 {
        let (v, alphabet) = random_pair(&mut rng, case);
        let system = parse(v.clone());
        let req: CheckRequestWire = serde_json::from_value(v).unwrap();
        let (sys2, limits) = input::parse_request(req).unwrap();
        let out = solver::solve(&sys2, &limits).unwrap();
        if let solver::KernelVerdict::Counterexample { trace, .. } = &out.verdict {
            let report = evidence::verify_trace(
                &sys2.alphabet,
                &sys2.specification,
                &sys2.implementation,
                trace,
            );
            assert!(
                report.accepted,
                "case {case}：求解器反例 {:?} 未通过独立验证：{report:?}",
                trace.iter().map(|i| &alphabet[*i as usize]).collect::<Vec<_>>()
            );
            assert!(report.spec_reachable.is_empty());
            assert!(!report.impl_reachable.is_empty());
        }
        let _ = system;
    }
}

// ---- 极简确定性 LCG 与小系统随机生成（测试专用，不引入 rand 依赖） ----

struct SimpleRng(u64);

impl SimpleRng {
    fn next_u64(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next_u64() % n as u64) as usize
    }
    fn chance(&mut self, p: u64) -> bool {
        self.next_u64() % 100 < p
    }
}

fn random_pair(rng: &mut SimpleRng, case: usize) -> (serde_json::Value, Vec<String>) {
    let alphabet: Vec<String> = ["a", "b"].iter().map(|s| s.to_string()).collect();
    let make_side = |rng: &mut SimpleRng, prefix: &str| {
        let n = 2 + rng.below(3);
        let states: Vec<String> = (0..n).map(|i| format!("{prefix}{i}")).collect();
        let mut edges = Vec::new();
        for s in &states {
            for a in &alphabet {
                if rng.chance(40) {
                    let t = &states[rng.below(n)];
                    edges.push(serde_json::json!({ "source": s, "action": a, "target": t }));
                }
            }
        }
        serde_json::json!({
            "name": format!("{prefix}-{case}"),
            "states": states,
            "initial_states": [states[0]],
            "edges": edges
        })
    };
    let spec = make_side(rng, "s");
    let impls = make_side(rng, "i");
    let v = serde_json::json!({
        "observable_actions": alphabet,
        "specification": spec,
        "implementation": impls
    });
    (v, alphabet)
}
