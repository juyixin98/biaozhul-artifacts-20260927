//! 求解内核语义测试。
//!
//! 关键要求：参考答案不能由被测核心自己生成。因此本文件实现一个**独立的短迹穷举预言机**
//! （朴素 BFS 展开，按长度/字典序枚举可观察迹，用自己的 τ 闭包判断接受/拒绝），
//! 用它逐迹对照被测求解器的结论：
//! * 求解器说 included  → 预言机枚举的每条被实现接受的迹，规格都必须接受；
//! * 求解器给反例迹 σ  → 预言机必须确认“实现接受 σ、规格拒绝 σ”，
//!   且不存在更短或同长度字典序更小的反例（即 σ 是预言机找到的第一条反例）。

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::path::PathBuf;

use serde_json::Value;
use weak_trace_inclusion::input;
use weak_trace_inclusion::model::CheckRequestWire;
use weak_trace_inclusion::solver::{self, KernelVerdict, UnknownReason};

fn fixture_path(name: &str) -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("tests/fixtures");
    p.push(name);
    p
}

fn load_request(name: &str) -> (CheckRequestWire, Value) {
    let raw = std::fs::read_to_string(fixture_path(name)).unwrap();
    let value: Value = serde_json::from_str(&raw).unwrap();
    let req: CheckRequestWire = serde_json::from_value(value.clone()).unwrap();
    (req, value)
}

// ===================== 独立预言机（不依赖求解器内部结构） =====================

/// 朴素可达状态集（名字集合），方法与求解器刻意不同：用 BTreeSet<String> 直接操作。
struct NaiveLts {
    initials: BTreeSet<String>,
    tau_out: std::collections::BTreeMap<String, BTreeSet<String>>,
    obs_out: std::collections::BTreeMap<(String, String), BTreeSet<String>>,
}

fn build_naive(value: &Value, alphabet: &[String]) -> NaiveLts {
    let alphabet_set: BTreeSet<&str> = alphabet.iter().map(String::as_str).collect();
    let hidden: BTreeSet<&str> = value["hidden_actions"]
        .as_array()
        .map(|a| a.iter().map(|v| v.as_str().unwrap()).collect())
        .unwrap_or_default();
    let mut states: BTreeSet<String> = BTreeSet::new();
    for s in value["states"].as_array().unwrap() {
        states.insert(s.as_str().unwrap().to_owned());
    }
    let mut initials = BTreeSet::new();
    for s in value["initial_states"].as_array().unwrap() {
        initials.insert(s.as_str().unwrap().to_owned());
    }
    let mut tau_out: std::collections::BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    let mut obs_out: std::collections::BTreeMap<(String, String), BTreeSet<String>> =
        BTreeMap::new();
    for e in value["edges"].as_array().unwrap() {
        let src = e["source"].as_str().unwrap();
        let act = e["action"].as_str().unwrap();
        let dst = e["target"].as_str().unwrap();
        states.insert(src.to_owned());
        states.insert(dst.to_owned());
        if alphabet_set.contains(act) {
            obs_out
                .entry((src.to_owned(), act.to_owned()))
                .or_default()
                .insert(dst.to_owned());
        } else {
            assert!(hidden.contains(act), "预言机构造前提不成立");
            tau_out.entry(src.to_owned()).or_default().insert(dst.to_owned());
        }
    }
    NaiveLts {
        initials,
        tau_out,
        obs_out,
    }
}

fn tau_closure(lts: &NaiveLts, start: &BTreeSet<String>) -> BTreeSet<String> {
    let mut reached = start.clone();
    let mut queue: VecDeque<String> = start.iter().cloned().collect();
    while let Some(u) = queue.pop_front() {
        if let Some(nexts) = lts.tau_out.get(&u) {
            for v in nexts {
                if reached.insert(v.clone()) {
                    queue.push_back(v.clone());
                }
            }
        }
    }
    reached
}

/// 沿一条可观察迹（动作名）的弱可达末态集。
fn weak_reachable(lts: &NaiveLts, trace: &[String]) -> BTreeSet<String> {
    let mut cur = tau_closure(lts, &lts.initials);
    for a in trace {
        let mut direct: BTreeSet<String> = BTreeSet::new();
        for s in &cur {
            if let Some(ts) = lts.obs_out.get(&(s.clone(), a.clone())) {
                direct.extend(ts.iter().cloned());
            }
        }
        cur = tau_closure(lts, &direct);
    }
    cur
}

/// 预言机的判定：按长度、再按字母表声明序做字典序 BFS，返回第一条反例（若有），
/// 枚举到 `max_len` 为止。
struct OracleVerdict {
    first_counterexample: Option<Vec<String>>,
    accepted_traces: Vec<Vec<String>>,
}

fn oracle_first_ce(
    spec: &NaiveLts,
    impls: &NaiveLts,
    alphabet: &[String],
    max_len: usize,
) -> OracleVerdict {
    let mut first: Option<Vec<String>> = None;
    let mut accepted = Vec::new();
    // 队列元素 (trace)；空迹先判定。
    let mut queue: VecDeque<Vec<String>> = VecDeque::new();
    queue.push_back(Vec::new());
    while let Some(trace) = queue.pop_front() {
        let i_reach = weak_reachable(impls, &trace);
        let s_reach = weak_reachable(spec, &trace);
        if !i_reach.is_empty() {
            accepted.push(trace.clone());
        }
        if !i_reach.is_empty() && s_reach.is_empty() && first.is_none() {
            first = Some(trace.clone());
        }
        if trace.len() < max_len {
            for a in alphabet {
                let mut next = trace.clone();
                next.push(a.clone());
                queue.push_back(next);
            }
        }
    }
    OracleVerdict {
        first_counterexample: first,
        accepted_traces: accepted,
    }
}

/// 用预言机全量核对“included”：长度 ≤ max_len 内每条实现接受迹，规格都接受。
/// 返回枚举到的实现接受迹数量，供调用方断言穷举确实发生了。
fn assert_oracle_agrees_included(
    spec: &NaiveLts,
    impls: &NaiveLts,
    alphabet: &[String],
    max_len: usize,
) -> usize {
    let oracle = oracle_first_ce(spec, impls, alphabet, max_len);
    assert!(
        oracle.first_counterexample.is_none(),
        "预言机在长度 {max_len} 内找到了反例 {:?}，但求解器声称 included",
        oracle.first_counterexample
    );
    oracle.accepted_traces.len()
}

fn solve(req: CheckRequestWire) -> solver::KernelOutput {
    let (system, limits) = input::parse_request(req).unwrap();
    solver::solve(&system, &limits).unwrap()
}

// ===================== 场景 1：隐藏内部步骤，弱包含成立 =====================

#[test]
fn hidden_internal_steps_are_included() {
    let (req, raw) = load_request("hidden_internal_steps.json");
    let alphabet: Vec<String> = raw["observable_actions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_owned())
        .collect();
    let spec = build_naive(&raw["specification"], &alphabet);
    let impls = build_naive(&raw["implementation"], &alphabet);

    let out = solve(req);
    assert_eq!(
        out.verdict,
        KernelVerdict::Included,
        "实现的可观察语言 (ab)* 应被规格 tick?a,b-循环 弱包含"
    );

    // 独立预言机穷举到长度 6：所有被实现接受的迹规格都必须接受。
    assert_oracle_agrees_included(&spec, &impls, &alphabet, 6);

    // 关键中间状态：规格闭包二元组应反映 tick 自循环展开（s0 闭包={s0,s1}）。
    assert!(out.stats.spec_closure_pairs >= 4);
    assert_eq!(out.stats.impl_states, 2);
}

/// 同一系统但去掉实现侧对齐：实现直接产生 c —— 最短反例必须是 ["c"]。
#[test]
fn hidden_steps_do_not_hide_extra_observable() {
    let (mut req, raw) = load_request("hidden_internal_steps.json");
    req.implementation.edges.push(weak_trace_inclusion::model::EdgeWire {
        id: Some("i-c".into()),
        source: "i0".into(),
        action: "c".into(),
        target: "i1".into(),
    });
    let alphabet: Vec<String> = raw["observable_actions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_owned())
        .collect();
    let mut impl_value = raw["implementation"].clone();
    impl_value["edges"].as_array_mut().unwrap().push(serde_json::json!({
        "id": "i-c", "source": "i0", "action": "c", "target": "i1"
    }));
    let spec = build_naive(&raw["specification"], &alphabet);
    let impls = build_naive(&impl_value, &alphabet);

    let out = solve(req);
    match &out.verdict {
        KernelVerdict::Counterexample { trace, .. } => {
            let names: Vec<&str> = trace.iter().map(|&i| alphabet[i as usize].as_str()).collect();
            assert_eq!(names, vec!["c"], "最短反例必须恰为单步 c");
        }
        other => panic!("期望 counterexample，实际 {other:?}"),
    }
    let oracle = oracle_first_ce(&spec, &impls, &alphabet, 5);
    assert_eq!(
        oracle.first_counterexample,
        Some(vec!["c".to_owned()]),
        "预言机的第一条反例也必须是 c"
    );
}

// ===================== 场景 2：错误额外输出，最短反例 ["a","b"] =====================

#[test]
fn extra_output_gives_shortest_counterexample_ab() {
    let (req, raw) = load_request("extra_output.json");
    let alphabet: Vec<String> = raw["observable_actions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_owned())
        .collect();
    let spec = build_naive(&raw["specification"], &alphabet);
    let impls = build_naive(&raw["implementation"], &alphabet);

    let out = solve(req);
    let trace = match &out.verdict {
        KernelVerdict::Counterexample {
            trace,
            impl_reach,
            spec_reach,
        } => {
            assert!(!impl_reach.is_empty(), "反例处实现侧必须可达");
            assert!(spec_reach.is_empty(), "反例处规格侧必须不可达");
            trace.clone()
        }
        other => panic!("期望 counterexample，实际 {other:?}"),
    };
    let names: Vec<String> = trace.iter().map(|&i| alphabet[i as usize].clone()).collect();
    assert_eq!(names, vec!["a", "b"], "最短反例应为 [a,b]，而不是 b（b 在初始态不可达）");

    // 逐条短迹对照：[]、[a]、[b]、[a,a]、[a,b] 的两侧接受差异。
    let cases: &[(Vec<&str>, bool, bool)] = &[
        (vec![], true, true),
        (vec!["a"], true, true),
        (vec!["b"], false, false),
        (vec!["a", "a"], false, false),
        (vec!["a", "b"], true, false),
    ];
    for (tr, impl_accept, spec_accept) in cases {
        let tr_owned: Vec<String> = tr.iter().map(|s| s.to_string()).collect();
        let i = weak_reachable(&impls, &tr_owned);
        let s = weak_reachable(&spec, &tr_owned);
        assert_eq!(!i.is_empty(), *impl_accept, "实现对 {tr:?} 接受性不符");
        assert_eq!(!s.is_empty(), *spec_accept, "规格对 {tr:?} 接受性不符");
    }

    // 预言机的第一条反例也必须是 [a,b]。
    let oracle = oracle_first_ce(&spec, &impls, &alphabet, 4);
    assert_eq!(
        oracle.first_counterexample,
        Some(vec!["a".to_owned(), "b".to_owned()])
    );
}

// ===================== 场景 3：不可达分支不得产生反例 =====================

#[test]
fn unreachable_branch_is_ignored() {
    let (req, raw) = load_request("unreachable_branch.json");
    let alphabet: Vec<String> = raw["observable_actions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_owned())
        .collect();
    let spec = build_naive(&raw["specification"], &alphabet);
    let impls = build_naive(&raw["implementation"], &alphabet);

    let out = solve(req);
    assert_eq!(
        out.verdict,
        KernelVerdict::Included,
        "i_dead 上的 c 分支从初态不可达，不构成反例"
    );
    assert_oracle_agrees_included(&spec, &impls, &alphabet, 6);

    // 若错误地把边的存在当作“可能迹”（不做可达性），就会误报 c；这里显式断言 c 被拒绝于实现侧。
    let c = vec!["c".to_owned()];
    assert!(weak_reachable(&impls, &c).is_empty(), "实现侧从初态不可产生 c");
}

// ===================== 不把“单步动作相同”当等价 =====================

/// 两个实现状态出边标签完全相同（都有 a），但继续展开后行为不同。
/// 朴素“按一步签名商集化”的算法会把它们合并，从而漏掉反例；精确子集对搜索不会。
#[test]
fn equal_one_step_signatures_must_not_be_merged() {
    let req_json = serde_json::json!({
        "run_id": "no-signature-quotient",
        "observable_actions": ["a", "x"],
        "specification": {
            "name": "spec",
            "states": ["s0", "s1"],
            "initial_states": ["s0"],
            "edges": [
                { "source": "s0", "action": "a", "target": "s1" },
                { "source": "s1", "action": "a", "target": "s1" }
            ]
        },
        "implementation": {
            "name": "impl-fork",
            "states": ["q0", "q1", "q2", "q3"],
            "initial_states": ["q0"],
            "edges": [
                { "source": "q0", "action": "a", "target": "q1" },
                { "source": "q0", "action": "a", "target": "q2" },
                { "source": "q1", "action": "a", "target": "q3" },
                { "source": "q2", "action": "a", "target": "q2" },
                { "source": "q3", "action": "x", "target": "q3" }
            ]
        }
    });
    let req: CheckRequestWire = serde_json::from_value(req_json.clone()).unwrap();
    let alphabet = vec!["a".to_owned(), "x".to_owned()];
    let spec = build_naive(&req_json["specification"], &alphabet);
    let impls = build_naive(&req_json["implementation"], &alphabet);

    let out = solve(req);
    // q1 与 q2 都只有一条 a 出边（单步签名相同），但 q1 --a--> q3 能产生 x。
    // 最短反例 [a, a, x]。
    let trace = match &out.verdict {
        KernelVerdict::Counterexample { trace, .. } => trace.clone(),
        other => panic!("期望 counterexample，实际 {other:?}"),
    };
    let names: Vec<String> = trace.iter().map(|&i| alphabet[i as usize].clone()).collect();
    assert_eq!(names, vec!["a", "a", "x"]);

    let oracle = oracle_first_ce(&spec, &impls, &alphabet, 4);
    assert_eq!(
        oracle.first_counterexample,
        Some(vec!["a".to_owned(), "a".to_owned(), "x".to_owned()])
    );
}

// ===================== 状态爆炸 → unknown =====================

#[test]
fn resource_exhaustion_returns_unknown_not_included() {
    // 规格对 a,b 都是自环（实际语言是全部迹——真实答案是 included），
    // 但实现是深度二叉树，每一层产生新的子集对；小预算下搜索必然先耗尽，
    // 此时必须返回 unknown，而不是草率给出 included。
    let mut states: Vec<String> = Vec::new();
    let mut edges: Vec<Value> = Vec::new();
    let depth = 10;
    for d in 0..depth {
        for k in 0..(1usize << d) {
            let src = format!("q{d}_{k}");
            states.push(src.clone());
            for (bit, act) in ["a", "b"].iter().enumerate() {
                let dst = format!("q{}_{}", d + 1, k * 2 + bit);
                edges.push(serde_json::json!({
                    "source": src, "action": act, "target": dst
                }));
            }
        }
    }
    let req_json = serde_json::json!({
        "observable_actions": ["a", "b"],
        "specification": {
            "name": "spec-all",
            "states": ["s0"],
            "initial_states": ["s0"],
            "edges": [
                { "source": "s0", "action": "a", "target": "s0" },
                { "source": "s0", "action": "b", "target": "s0" }
            ]
        },
        "implementation": {
            "name": "impl-tree",
            "states": states,
            "initial_states": ["q0_0"],
            "edges": edges
        },
        "limits": { "max_search_nodes": 5 }
    });

    let req: CheckRequestWire = serde_json::from_value(req_json).unwrap();
    let out = solve(req);
    match out.verdict {
        KernelVerdict::Unknown {
            reason: UnknownReason::SearchNodeLimit,
        } => {}
        other => panic!("期望 search_node_limit 的 unknown，实际 {other:?}"),
    }
    assert!(
        out.stats.search_nodes_visited >= 1,
        "诊断应记录已访问的搜索节点数"
    );
}

/// 极小闭包预算：τ 环链导致闭包二元组超预算。
#[test]
fn closure_budget_exhaustion_returns_unknown() {
    let req_json = serde_json::json!({
        "observable_actions": ["a"],
        "specification": {
            "name": "spec",
            "states": ["s0", "s1"],
            "initial_states": ["s0"],
            "hidden_actions": ["tau"],
            "edges": [
                { "source": "s0", "action": "tau", "target": "s1" },
                { "source": "s1", "action": "tau", "target": "s0" },
                { "source": "s0", "action": "a", "target": "s0" }
            ]
        },
        "implementation": {
            "name": "impl",
            "states": ["i0"],
            "initial_states": ["i0"],
            "edges": [
                { "source": "i0", "action": "a", "target": "i0" }
            ]
        },
        "limits": { "max_closure_pairs": 1 }
    });
    let req: CheckRequestWire = serde_json::from_value(req_json).unwrap();
    let out = solve(req);
    // 规格单个状态闭包至少 2 个二元组，预算 1 必然耗尽。
    assert!(matches!(
        out.verdict,
        KernelVerdict::Unknown {
            reason: UnknownReason::ClosurePairLimit
        }
    ));
}
