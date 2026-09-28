//! 证据验证模块（独立于求解内核）。
//!
//! 求解器给出一条反例迹后，本模块用**自己重新实现**的 τ 闭包与弱模拟来回答两个问题：
//! 1. 实现侧是否确实接受该迹（末态集非空，并抽取一条逐步可核对的具体路径）；
//! 2. 规格侧是否确实拒绝该迹（末态集为空）。
//!
//! 它不读取求解器的闭包表/回放映射/弱像表，只共享被检查的 [`Lts`] 数据结构本身；
//! 这样证据结论不是“被测核心自己给自己作证”。具体路径中的每条边都按规范索引回查
//! 源/目标/动作，任何不一致都会让报告 `accepted = false`。

use serde::Serialize;

use crate::model::{EdgeLabel, Lts};

#[derive(Debug, Clone, Serialize)]
pub struct EdgeRefWire {
    /// 规范边索引（与输入中按规范序给出的编号一致）。
    pub edge: u32,
    /// 调用方提供的边 id（若有）。
    pub id: Option<String>,
    pub source: String,
    pub action: String,
    pub target: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct HopWire {
    pub action: String,
    /// 选定具体状态后、观察动作之前经过的静默边。
    pub tau_before: Vec<EdgeRefWire>,
    /// 唯一的可观察边。
    pub observable: EdgeRefWire,
    /// 观察动作之后到达下一选定状态的静默边。
    pub tau_after: Vec<EdgeRefWire>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ConcretePathWire {
    pub initial_state: String,
    /// 从声明初态到第一个观察动作起点之前的静默段。
    pub prefix_tau: Vec<EdgeRefWire>,
    pub hops: Vec<HopWire>,
    pub final_state: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct CheckItem {
    pub name: &'static str,
    pub passed: bool,
    pub reason: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct EvidenceReport {
    /// 所有检查项是否全部通过。
    pub accepted: bool,
    pub trace: Vec<String>,
    pub checks: Vec<CheckItem>,
    /// 实现侧沿迹可达的末态（全集，名字）。
    pub impl_reachable: Vec<String>,
    /// 规格侧沿迹可达的末态（全集，名字）；反例应为空集。
    pub spec_reachable: Vec<String>,
    /// 实现侧的一条具体接受路径（证据）。
    pub impl_witness: Option<ConcretePathWire>,
}

/// 验证用的独立 τ 闭包缓存：对每个状态各自做一次 DFS，代码路径与求解器完全独立。
struct LocalClosures<'a> {
    lts: &'a Lts,
    cache: Vec<Vec<u32>>,
}

impl<'a> LocalClosures<'a> {
    fn new(lts: &'a Lts) -> Self {
        Self {
            lts,
            cache: vec![Vec::new(); lts.num_states()],
        }
    }

    fn of(&mut self, s: u32) -> &[u32] {
        if self.cache[s as usize].is_empty() {
            let mut reached = vec![s];
            let mut seen = vec![false; self.lts.num_states()];
            seen[s as usize] = true;
            let mut stack = vec![s];
            while let Some(u) = stack.pop() {
                for &e in &self.lts.out[u as usize] {
                    let edge = &self.lts.edges[e as usize];
                    if matches!(edge.label, EdgeLabel::Tau { .. }) && !seen[edge.target as usize] {
                        seen[edge.target as usize] = true;
                        reached.push(edge.target);
                        stack.push(edge.target);
                    }
                }
            }
            reached.sort_unstable();
            self.cache[s as usize] = reached;
        }
        &self.cache[s as usize]
    }

    /// 从一组状态出发的联合闭包（升序去重），结果写入 `out`。
    fn of_set_into(&mut self, starts: &[u32], seen: &mut [bool], out: &mut Vec<u32>) {
        seen.fill(false);
        for &s in starts {
            for &t in self.of(s) {
                if !seen[t as usize] {
                    seen[t as usize] = true;
                    out.push(t);
                }
            }
        }
        out.sort_unstable();
    }
}

/// 独立弱像：C({t | ∃s∈X, s --a--> t})。
fn weak_image(
    lts: &Lts,
    closures: &mut LocalClosures<'_>,
    set: &[u32],
    action_idx: u32,
    seen: &mut [bool],
    direct: &mut Vec<u32>,
    out: &mut Vec<u32>,
) {
    direct.clear();
    seen.fill(false);
    for &s in set {
        for &t in &lts.obs_out[s as usize][action_idx as usize] {
            if !seen[t as usize] {
                seen[t as usize] = true;
                direct.push(t);
            }
        }
    }
    out.clear();
    closures.of_set_into(direct, seen, out);
}

/// 在 τ 图上做一次 BFS，返回从 `from` 到 `to` 的最短（按规范邻接序）边索引路径。
fn tau_path(lts: &Lts, from: u32, to: u32) -> Option<Vec<u32>> {
    if from == to {
        return Some(Vec::new());
    }
    let n = lts.num_states();
    let mut parent_state = vec![u32::MAX; n];
    let mut parent_edge = vec![u32::MAX; n];
    let mut discovered = vec![false; n];
    discovered[from as usize] = true;
    let mut queue = std::collections::VecDeque::new();
    queue.push_back(from);
    while let Some(u) = queue.pop_front() {
        if u == to {
            break;
        }
        for &e in &lts.out[u as usize] {
            let edge = &lts.edges[e as usize];
            if matches!(edge.label, EdgeLabel::Tau { .. }) && !discovered[edge.target as usize] {
                discovered[edge.target as usize] = true;
                parent_state[edge.target as usize] = u;
                parent_edge[edge.target as usize] = e;
                queue.push_back(edge.target);
            }
        }
    }
    if !discovered[to as usize] {
        return None;
    }
    let mut path = Vec::new();
    let mut cur = to;
    while cur != from {
        path.push(parent_edge[cur as usize]);
        cur = parent_state[cur as usize];
    }
    path.reverse();
    Some(path)
}

fn edge_ref(lts: &Lts, idx: u32) -> EdgeRefWire {
    let e = &lts.edges[idx as usize];
    EdgeRefWire {
        edge: idx,
        id: e.user_id.clone(),
        source: lts.state_name(e.source).to_owned(),
        action: e.label.display_name().to_owned(),
        target: lts.state_name(e.target).to_owned(),
    }
}

/// 沿一组动作模拟，返回每个前缀（含空前缀）之后的可达状态集。
fn simulate(lts: &Lts, actions: &[u32]) -> Vec<Vec<u32>> {
    let mut closures = LocalClosures::new(lts);
    let n = lts.num_states();
    let mut seen = vec![false; n];
    let mut direct = Vec::new();
    let mut buf = Vec::new();

    let mut layers = Vec::with_capacity(actions.len() + 1);
    let mut cur = Vec::new();
    closures.of_set_into(&lts.initial_states, &mut seen, &mut cur);
    layers.push(cur.clone());
    for &a in actions {
        weak_image(lts, &mut closures, &cur, a, &mut seen, &mut direct, &mut buf);
        cur.clone_from(&buf);
        layers.push(cur.clone());
    }
    layers
}

/// 为接受侧抽取一条逐步具体的路径。使用后向“可行状态”集合保证前向选择不会走进死胡同。
fn extract_witness(
    lts: &Lts,
    actions: &[u32],
    layers: &[Vec<u32>],
) -> Option<ConcretePathWire> {
    debug_assert_eq!(layers.len(), actions.len() + 1);
    let mut closures = LocalClosures::new(lts);

    // 后向可行集 V_k ⊆ layers[k]：从该状态出发确实能走完整条后缀。
    let mut viable: Vec<Vec<u32>> = Vec::with_capacity(actions.len() + 1);
    for _ in 0..actions.len() + 1 {
        viable.push(Vec::new());
    }
    viable[actions.len()] = layers[actions.len()].clone();
    for k in (0..actions.len()).rev() {
        let a = actions[k];
        for &s0 in &layers[k] {
            let mut ok = false;
            let closure_s0: Vec<u32> = closures.of(s0).to_vec();
            'outer: for s in closure_s0 {
                for &t in &lts.obs_out[s as usize][a as usize] {
                    let c = closures.of(t);
                    if c.iter().any(|q| viable[k + 1].binary_search(q).is_ok()) {
                        ok = true;
                        break 'outer;
                    }
                }
            }
            if ok {
                viable[k].push(s0);
            }
        }
    }

    // 选初态：按声明顺序找一个其 τ 闭包与 V_0 相交的初始状态。
    let mut initial = u32::MAX;
    let mut p = u32::MAX;
    'pick: for &i in &lts.initial_states {
        for q in closures.of(i) {
            if viable[0].binary_search(q).is_ok() {
                initial = i;
                p = *q;
                break 'pick;
            }
        }
    }
    if initial == u32::MAX {
        return None;
    }
    let prefix_edges = tau_path(lts, initial, p)?;

    let mut hops = Vec::new();
    for (k, &a) in actions.iter().enumerate() {
        // 从 p 出发，找 τ 可达的 s，s 有 a 边到 t，且 C(t) 与 V_{k+1} 相交。
        let closure_p = closures.of(p).to_vec();
        let mut chosen_s = u32::MAX;
        let mut chosen_t = u32::MAX;
        let mut chosen_q = u32::MAX;
        'find: for &s in &closure_p {
            for &t in &lts.obs_out[s as usize][a as usize] {
                for &q in closures.of(t) {
                    if viable[k + 1].binary_search(&q).is_ok() {
                        chosen_s = s;
                        chosen_t = t;
                        chosen_q = q;
                        break 'find;
                    }
                }
            }
        }
        if chosen_s == u32::MAX {
            return None;
        }
        let before = tau_path(lts, p, chosen_s)?;
        // 规范序下第一条 s --a--> t 的边。
        let obs_edge = lts.out[chosen_s as usize]
            .iter()
            .copied()
            .find(|&e| {
                let edge = &lts.edges[e as usize];
                matches!(&edge.label, EdgeLabel::Observable { action_idx, .. } if *action_idx == a)
                    && edge.target == chosen_t
            })?;
        let after = tau_path(lts, chosen_t, chosen_q)?;

        hops.push(HopWire {
            action: lts
                .edges
                .iter()
                .find_map(|e| match &e.label {
                    EdgeLabel::Observable { action_idx, name } if *action_idx == a => {
                        Some(name.clone())
                    }
                    _ => None,
                })
                .unwrap_or_default(),
            tau_before: before.iter().map(|&e| edge_ref(lts, e)).collect(),
            observable: edge_ref(lts, obs_edge),
            tau_after: after.iter().map(|&e| edge_ref(lts, e)).collect(),
        });
        p = chosen_q;
    }

    Some(ConcretePathWire {
        initial_state: lts.state_name(initial).to_owned(),
        prefix_tau: prefix_edges.iter().map(|&e| edge_ref(lts, e)).collect(),
        hops,
        final_state: lts.state_name(p).to_owned(),
    })
}

/// 校验抽取出来的具体路径：边索引存在、端点/动作一致、静默段确实是 τ、观察边与迹一致。
fn audit_witness(lts: &Lts, actions: &[u32], path: &ConcretePathWire) -> Result<(), String> {
    let initial_idx = lts
        .state_names
        .iter()
        .position(|n| n == &path.initial_state)
        .map(|i| i as u32)
        .ok_or_else(|| format!("witness 初态 {} 不存在", path.initial_state))?;
    if !lts.initial_states.contains(&initial_idx) {
        return Err(format!("witness 初态 {} 不是声明初态", path.initial_state));
    }
    if path.hops.len() != actions.len() {
        return Err(format!(
            "witness 跳数 {} 与迹长度 {} 不符",
            path.hops.len(),
            actions.len()
        ));
    }

    let check_edge = |r: &EdgeRefWire, expect_action: Option<u32>| -> Result<crate::model::Edge, String> {
        let e = lts
            .edges
            .get(r.edge as usize)
            .ok_or_else(|| format!("witness 引用了不存在的边索引 {}", r.edge))?
            .clone();
        if lts.state_name(e.source) != r.source
            || lts.state_name(e.target) != r.target
            || e.label.display_name() != r.action
        {
            return Err(format!(
                "边 {} 的源/目标/动作与系统记录不一致",
                r.edge
            ));
        }
        if let Some(want_idx) = expect_action {
            match &e.label {
                EdgeLabel::Observable { action_idx, .. } if *action_idx == want_idx => {}
                _ => return Err(format!("边 {} 不是期望的可观察动作", r.edge)),
            }
        } else if !e.label.is_tau() {
            return Err(format!("边 {} 应为静默边，实际是可观察边", r.edge));
        }
        Ok(e)
    };

    let mut current = initial_idx;
    let walk_tau = |current: &mut u32, seg: &[EdgeRefWire]| -> Result<(), String> {
        for r in seg {
            let e = check_edge(r, None)?;
            if e.source != *current {
                return Err(format!("路径不连通：边 {} 并非从当前状态出发", r.edge));
            }
            *current = e.target;
        }
        Ok(())
    };

    walk_tau(&mut current, &path.prefix_tau)?;
    for (k, hop) in path.hops.iter().enumerate() {
        walk_tau(&mut current, &hop.tau_before)?;
        let ob = check_edge(&hop.observable, Some(actions[k]))?;
        if ob.source != current {
            return Err(format!("第 {k} 跳观察边不连通"));
        }
        current = ob.target;
        walk_tau(&mut current, &hop.tau_after)?;
    }
    if lts.state_name(current) != path.final_state {
        return Err("witness 末态与记录不符".into());
    }
    Ok(())
}

fn names_of(lts: &Lts, set: &[u32]) -> Vec<String> {
    set.iter().map(|&s| lts.state_name(s).to_owned()).collect()
}

/// 证据验证入口。`trace` 为求解器给出的可观察动作索引序列（已与字母表对齐）。
#[must_use]
pub fn verify_trace(
    alphabet: &[String],
    spec: &Lts,
    impls: &Lts,
    trace: &[u32],
) -> EvidenceReport {
    let mut checks: Vec<CheckItem> = Vec::new();

    // 检查 1：迹中每个下标都是合法的可观察动作。
    let aligned = trace.iter().all(|&a| (a as usize) < alphabet.len());
    checks.push(CheckItem {
        name: "trace_actions_aligned",
        passed: aligned,
        reason: if aligned {
            "迹中全部动作均在已对齐的可观察字母表中".to_owned()
        } else {
            "迹中存在超出字母表范围的动作下标".to_owned()
        },
    });

    let trace_names: Vec<String> = trace
        .iter()
        .map(|&a| {
            alphabet
                .get(a as usize)
                .cloned()
                .unwrap_or_else(|| format!("<invalid:{a}>"))
        })
        .collect();

    let impl_layers = if aligned {
        Some(simulate(impls, trace))
    } else {
        None
    };
    let spec_layers = if aligned {
        Some(simulate(spec, trace))
    } else {
        None
    };

    let impl_final = impl_layers.as_ref().map(|l| l.last().unwrap().clone());
    let spec_final = spec_layers.as_ref().map(|l| l.last().unwrap().clone());

    let impl_accepts = impl_final.as_ref().is_some_and(|f| !f.is_empty());
    checks.push(CheckItem {
        name: "impl_accepts_trace",
        passed: impl_accepts,
        reason: match &impl_final {
            Some(f) if f.is_empty() => "独立模拟：实现侧沿该迹末态集为空（不接受）".to_owned(),
            Some(f) => format!(
                "独立模拟：实现侧沿该迹可达 {} 个状态：{}",
                f.len(),
                names_of(impls, f).join(", ")
            ),
            None => "迹含非法动作，未进行模拟".to_owned(),
        },
    });

    let spec_rejects = spec_final.as_ref().is_some_and(|f| f.is_empty());
    checks.push(CheckItem {
        name: "spec_rejects_trace",
        passed: spec_rejects,
        reason: match &spec_final {
            Some(f) if f.is_empty() => "独立模拟：规格侧沿该迹末态集为空（拒绝）".to_owned(),
            Some(f) => format!(
                "独立模拟：规格侧仍可达 {} 个状态：{}",
                f.len(),
                names_of(spec, f).join(", ")
            ),
            None => "迹含非法动作，未进行模拟".to_owned(),
        },
    });

    // 抽取并审计实现侧具体路径。
    let mut witness = None;
    let witness_ok = if impl_accepts {
        let layers = impl_layers.unwrap();
        match extract_witness(impls, trace, &layers) {
            Some(w) => match audit_witness(impls, trace, &w) {
                Ok(()) => {
                    checks.push(CheckItem {
                        name: "impl_witness_walk_consistent",
                        passed: true,
                        reason: format!(
                            "实现侧具体路径由 {} 条边组成，全部边引用与连接关系核对一致",
                            w.prefix_tau.len() + w.hops.iter().map(|h| 1 + h.tau_before.len() + h.tau_after.len()).sum::<usize>()
                        ),
                    });
                    witness = Some(w);
                    true
                }
                Err(msg) => {
                    checks.push(CheckItem {
                        name: "impl_witness_walk_consistent",
                        passed: false,
                        reason: format!("具体路径审计失败：{msg}"),
                    });
                    false
                }
            },
            None => {
                checks.push(CheckItem {
                    name: "impl_witness_walk_consistent",
                    passed: false,
                    reason: "末态集非空但无法抽取具体路径（内部矛盾）".to_owned(),
                });
                false
            }
        }
    } else {
        checks.push(CheckItem {
            name: "impl_witness_walk_consistent",
            passed: false,
            reason: "实现侧不接受该迹，无可核对路径".to_owned(),
        });
        false
    };

    let accepted = aligned && impl_accepts && spec_rejects && witness_ok;

    EvidenceReport {
        accepted,
        trace: trace_names,
        checks,
        impl_reachable: impl_final.map(|f| names_of(impls, &f)).unwrap_or_default(),
        spec_reachable: spec_final.map(|f| names_of(spec, &f)).unwrap_or_default(),
        impl_witness: witness,
    }
}
