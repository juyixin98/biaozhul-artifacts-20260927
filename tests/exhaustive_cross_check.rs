//! 穷举小标识空间对照：对每个夹具，从初标识出发枚举全部容量合法标识，
//! 独立参考实现（tests/common）给出可达真值，再逐目标调用内核 BFS 对照。
//!
//! 真值来自独立参考实现，不由被测内核生成；同时对每条参考最短路径做
//! 独立重放、令牌加权不变量与容量边界断言。

mod common;

use common::{load_fixture, RefNet};
use petri_reach::input::parse_json_net;
use petri_reach::kernel::{
    analyze_reachability, compute_invariants, conservation_residual, weighted_sum, Marking,
    ReachabilityDecision, ReachabilityOptions,
};
use petri_reach::verify::replay;

const FIXTURES: &[&str] = &["mutex.json", "producer_consumer.json", "deadlock.json"];

/// 枚举每个库所 0..=capacity 的笛卡尔积（全部容量合法标识）。
fn all_markings(capacities: &[i64]) -> Vec<Vec<i64>> {
    let mut out: Vec<Vec<i64>> = vec![vec![]];
    for &cap in capacities {
        let mut next = Vec::new();
        for m in &out {
            for tok in 0..=cap {
                let mut mm = m.clone();
                mm.push(tok);
                next.push(mm);
            }
        }
        out = next;
    }
    out
}

#[test]
fn kernel_bfs_matches_independent_reference_on_every_marking() {
    for fixture in FIXTURES {
        let v = load_fixture(fixture);
        let text = serde_json::to_string(&v).unwrap();
        let (net, _) = parse_json_net(&text).unwrap();
        let rn = RefNet::from_fixture(&v);
        let m0 = RefNet::initial_from_fixture(&v);

        let reference_set = rn.exhaustive_reachable(&m0);

        // 不变量候选：独立验证残差确实为 0（候选必须真是守恒律）。
        let inv_report = compute_invariants(&net, Default::default());
        let genuine: Vec<Vec<i64>> = inv_report
            .candidates
            .iter()
            .filter(|c| conservation_residual(&net, &c.weights).iter().all(|r| *r == 0))
            .map(|c| c.weights.clone())
            .collect();

        let opts = ReachabilityOptions {
            // 这些夹具状态空间都很小，给一个远大于空间的上限，保证不被截断。
            state_limit: 1_000_000,
            ..Default::default()
        };

        for target in all_markings(&rn.capacities) {
            let r = analyze_reachability(&net, &Marking(m0.clone()), &Marking(target.clone()), &opts);
            let ref_reachable = reference_set.contains(&target);
            match (r.decision, ref_reachable) {
                (ReachabilityDecision::Reachable, true) => {
                    // 有证据，且证据路径独立重放合法、终点精确一致。
                    let cert = r.certificate.as_ref().expect("reachable must carry a certificate");
                    let kernel_replay = replay(&net, &m0, &cert.transition_sequence)
                        .unwrap_or_else(|e| panic!("{fixture}: replay failed: {e:?}"));
                    assert!(kernel_replay.valid, "{fixture}: replay must be valid");
                    assert_eq!(
                        kernel_replay.final_marking, target,
                        "{fixture}: replay must land exactly on target"
                    );

                    // 参考实现也必须认可这条路径，并给出自己的（可能不同的）最短路径。
                    let ref_path = rn
                        .ref_path(&m0, &target)
                        .unwrap_or_else(|| panic!("{fixture}: reference says target reachable but gave no path"));
                    let ref_replay = replay(&net, &m0, &ref_path)
                        .unwrap_or_else(|e| panic!("{fixture}: reference path replay failed: {e:?}"));
                    assert_eq!(ref_replay.final_marking, target);
                    // BFS 给的路径必须是最短的之一。
                    assert!(
                        cert.path_length <= ref_path.len(),
                        "{fixture}: kernel path {} longer than reference shortest {}",
                        cert.path_length,
                        ref_path.len()
                    );

                    // 令牌加权不变量：路径上每个标识对每个真实守恒律加权和恒定。
                    for w in &genuine {
                        let base = weighted_sum(w, &m0);
                        for (step, m) in kernel_replay.reached.iter().enumerate() {
                            assert_eq!(
                                weighted_sum(w, m),
                                base,
                                "{fixture}: invariant {w:?} changed at step {step}"
                            );
                        }
                    }
                }
                (ReachabilityDecision::Unreachable, false) => {
                    // 容量模型内穷尽/不变量否决，必须给出依据且无伪造证据。
                    assert!(r.certificate.is_none(), "{fixture}: unreachable must not carry a certificate");
                    assert!(
                        r.basis == "capacity_state_space_exhausted"
                            || r.basis == "p_invariant_conservation_witness",
                        "{fixture}: unreachable needs a concrete basis, got {}",
                        r.basis
                    );
                }
                (decision, ref_reachable) => panic!(
                    "{fixture}: kernel decision {decision:?} disagrees with independent reference (reachable={ref_reachable}) for target {target:?}"
                ),
            }
        }

        // 容量边界：参考可达集里没有任何标识越界（发射从不截断）。
        for m in &reference_set {
            for (i, tok) in m.iter().enumerate() {
                assert!(0 <= *tok && *tok <= rn.capacities[i], "{fixture}: capacity violated {m:?}");
            }
        }
    }
}

#[test]
fn exhaustive_reference_is_itself_consistent() {
    // 参考实现自检：初标识在集合内；集合对合法发射封闭；禁发变迁不会推进状态。
    for fixture in FIXTURES {
        let v = load_fixture(fixture);
        let rn = RefNet::from_fixture(&v);
        let m0 = RefNet::initial_from_fixture(&v);
        let set = rn.exhaustive_reachable(&m0);
        assert!(set.contains(&m0), "{fixture}: initial must be reachable");
        for m in &set {
            for ti in 0..rn.transition_names.len() {
                if let Some(n) = rn.ref_fire(m, ti) {
                    assert!(set.contains(&n), "{fixture}: reachable set not closed under legal firing");
                }
            }
        }
    }
}
