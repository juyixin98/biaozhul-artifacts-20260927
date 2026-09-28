//! 三个夹具网的具体行为断言（互斥资源、生产消费、死锁网）。
//! 期望值来自手工推导，可达性真值由独立参考实现 tests/common 穷举给出。

mod common;

use std::collections::BTreeMap;

use common::{load_fixture, RefNet};
use petri_reach::input::parse_json_net;
use petri_reach::kernel::{
    analyze_reachability, compute_invariants, Marking, ReachabilityDecision, ReachabilityOptions,
};
use petri_reach::verify::replay;

fn marking_of(rn: &RefNet, entries: &[(&str, i64)]) -> Vec<i64> {
    let mut m: BTreeMap<String, i64> = BTreeMap::new();
    for (k, v) in entries {
        m.insert((*k).to_string(), *v);
    }
    rn.initial_from_map(&m)
}

fn load_net(name: &str) -> (petri_reach::kernel::Net, RefNet, serde_json::Value) {
    let v = load_fixture(name);
    let text = serde_json::to_string(&v).unwrap();
    let (net, _) = parse_json_net(&text).expect("fixture must parse through the input layer");
    let rn = RefNet::from_fixture(&v);
    (net, rn, v)
}

#[test]
fn mutex_critical_sections_are_mutually_exclusive() {
    let (net, rn, _v) = load_net("mutex.json");
    let m0 = vec![1, 1, 0, 1, 0];

    // 1) 单个进入临界区可达，并给出具体路径。
    let target = marking_of(&rn, &[("crit_a", 1), ("idle_b", 1)]);
    let r = analyze_reachability(&net, &Marking(m0.clone()), &Marking(target.clone()), &ReachabilityOptions::default());
    assert_eq!(r.decision, ReachabilityDecision::Reachable);
    let cert = r.certificate.clone().unwrap();
    assert_eq!(cert.transition_sequence, vec!["enter_a"]);

    // 2) 两个临界区同时占用：容量空间允许该标识，但互斥使其不可达。
    let both = marking_of(&rn, &[("crit_a", 1), ("crit_b", 1)]);
    let r = analyze_reachability(&net, &Marking(m0.clone()), &Marking(both.clone()), &ReachabilityOptions::default());
    assert_eq!(r.decision, ReachabilityDecision::Unreachable, "both critical sections held must be unreachable");
    // 独立参考实现必须一致。
    let reachable = rn.exhaustive_reachable(&m0);
    assert!(!reachable.contains(&both));
    assert!(reachable.contains(&target));

    // 3) P 不变量候选必须包含 free+crit_a+crit_b = 1（资源守恒）。
    let inv = compute_invariants(&net, Default::default());
    let free_idx = rn.place_names.iter().position(|n| n == "free").unwrap();
    let ca = rn.place_names.iter().position(|n| n == "crit_a").unwrap();
    let cb = rn.place_names.iter().position(|n| n == "crit_b").unwrap();
    let mut expected = vec![0i64; rn.place_names.len()];
    expected[free_idx] = 1;
    expected[ca] = 1;
    expected[cb] = 1;
    assert!(
        inv.candidates.iter().any(|c| c.weights == expected),
        "resource invariant {expected:?} missing from candidates: {:?}",
        inv.candidates.iter().map(|c| &c.weights).collect::<Vec<_>>()
    );

    // 4) 证书路径独立重放合法。
    let replay = replay(&net, &m0, &cert.transition_sequence).unwrap();
    assert!(replay.valid);
    assert_eq!(replay.final_marking, target);
}

#[test]
fn producer_consumer_capacity_blocks_overflow_without_truncation() {
    let (net, rn, _v) = load_net("producer_consumer.json");
    let m0 = vec![0, 1, 1, 0];
    let reachable = rn.exhaustive_reachable(&m0);

    let buf = rn.place_names.iter().position(|n| n == "buffer").unwrap();
    let cons = rn.place_names.iter().position(|n| n == "consumed").unwrap();

    // 手工推导（produce +2，consume -3，容量 5）：
    // 0->2->4，4 时 consume->1，1->3，3->5；3 时 consume->0；5 时 consume->2。
    // 可达 buffer 值恰好是 {0,1,2,3,4,5}，全部满足 0<=buffer<=5。
    let reachable_levels: std::collections::BTreeSet<i64> =
        reachable.iter().map(|m| m[buf]).collect();
    let expected: std::collections::BTreeSet<i64> = (0..=5).collect();
    assert_eq!(reachable_levels, expected, "reachable buffer levels must be exactly 0..=5");
    // 容量边界在整个参考可达集里处处成立（无任何截断）。
    for m in &reachable {
        assert!(0 <= m[buf] && m[buf] <= rn.capacities[buf]);
        assert!(0 <= m[cons] && m[cons] <= rn.capacities[cons]);
    }

    // 容量阻断：buffer=4 时 produce 会到 6，必须被禁止，且原标识不变。
    // ready_p/ready_c 上是自环，始终持有 1 个令牌。
    use petri_reach::kernel::{fire, why_not_enabled};
    let produce_idx = net.transition_index("produce").unwrap();
    let m4 = Marking(marking_of(&rn, &[
        ("buffer", 4),
        ("consumed", 1),
        ("ready_p", 1),
        ("ready_c", 1),
    ]));
    let blocked = why_not_enabled(&net, &m4, produce_idx)
        .unwrap()
        .expect("produce at buffer=4 must be blocked");
    assert_eq!(blocked.capacity_overflows.len(), 1);
    assert_eq!(blocked.capacity_overflows[0].place, "buffer");
    assert_eq!(blocked.capacity_overflows[0].resulting, 6);
    assert_eq!(blocked.capacity_overflows[0].capacity, 5);
    assert!(blocked.insufficient_inputs.is_empty());
    let before = m4.0.clone();
    assert!(fire(&net, &m4, produce_idx).is_err());
    assert_eq!(m4.0, before, "marking untouched after refused fire");

    // buffer=5 恰好到容量上界：可达，路径 produce produce consume produce produce。
    let t5 = Marking(marking_of(&rn, &[
        ("buffer", 5),
        ("consumed", 1),
        ("ready_p", 1),
        ("ready_c", 1),
    ]));
    let r5 = analyze_reachability(&net, &Marking(m0.clone()), &t5, &ReachabilityOptions::default());
    assert_eq!(r5.decision, ReachabilityDecision::Reachable);
    let cert5 = r5.certificate.unwrap();
    assert_eq!(
        cert5.transition_sequence,
        vec!["produce", "produce", "consume", "produce", "produce"]
    );
    // 独立重放这条路径必须合法且终点一致。
    let replay = replay(&net, &m0, &cert5.transition_sequence).unwrap();
    assert!(replay.valid);
    assert_eq!(replay.final_marking, t5.0);
}


#[test]
fn deadlock_marking_is_reachable_but_trapped() {
    let (net, rn, _v) = load_net("deadlock.json");
    let m0 = vec![1, 1, 1, 1, 0, 0, 0, 0];

    // 双方各持一把锁、各等第二把：(Wa, Wb) = (1,1)。
    let deadlocked = marking_of(&rn, &[("Wa", 1), ("Wb", 1)]);
    let reachable = rn.exhaustive_reachable(&m0);
    assert!(reachable.contains(&deadlocked), "deadlocked marking must be reachable");

    // 该标识下参考实现报告零使能变迁。
    let enabled = rn.enabled_names(&deadlocked);
    assert!(enabled.is_empty(), "no transition must be enabled in deadlock, got {enabled:?}");

    // 内核能给出到达死锁的路径（a_take_ra 然后 b_take_rb，顺序可互换）。
    let r = analyze_reachability(
        &net,
        &Marking(m0.clone()),
        &Marking(deadlocked.clone()),
        &ReachabilityOptions::default(),
    );
    assert_eq!(r.decision, ReachabilityDecision::Reachable);
    let cert = r.certificate.unwrap();
    let seq: Vec<&str> = cert.transition_sequence.iter().map(String::as_str).collect();
    assert!(
        seq == vec!["a_take_ra", "b_take_rb"] || seq == vec!["b_take_rb", "a_take_ra"],
        "unexpected path into deadlock: {seq:?}"
    );

    // 从死锁标识回不到初标识（不可达，穷举证明）。
    let back = analyze_reachability(&net, &Marking(deadlocked), &Marking(m0), &ReachabilityOptions::default());
    assert_eq!(back.decision, ReachabilityDecision::Unreachable);
    assert!(back.certificate.is_none());
}
