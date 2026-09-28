//! 显式容量上界下的标识可达性分析（正向状态空间 BFS）。
//!
//! 完备性说明（行为契约 4）：每个库所都带有限容量 `c_p`，因此状态空间大小至多为
//! `Π(c_p + 1)`，是有限的；穷尽该空间给出的不可达结论在**容量模型内**是完备的。
//! 但容量网的不可达结论**不能**外推为无界网的完整可达性判定——无界网可能存在
//! 容量模型之外的 k-有界/无界执行路径。配置里的 `state_limit` 触顶时返回
//! `Inconclusive` 而不是“不可达”。

use std::collections::HashMap;
use std::collections::VecDeque;

use serde::Serialize;
use tracing::{debug, info};

use super::fire;
use super::invariants::{
    compute_invariants, weighted_sum, InvariantBounds, PInvariant,
};
use super::model::{Marking, Net};

/// 可达性判定三值结果。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ReachabilityDecision {
    /// 找到合法发射序列，从初标识到达目标。
    Reachable,
    /// 容量状态空间穷尽，目标不在其中（仅对容量模型成立）。
    Unreachable,
    /// 被状态数上限截断，或被有界 P 不变量枚举的完备性边界限制，不能下结论。
    Inconclusive,
}

#[derive(Debug, Clone)]
pub struct ReachabilityOptions {
    /// BFS 访问标识数上限；触顶返回 Inconclusive。
    pub state_limit: u64,
    /// 是否先做 P 不变量候选的加权守恒快速否决。
    pub use_invariant_precheck: bool,
    pub invariant_bounds: InvariantBounds,
}

impl Default for ReachabilityOptions {
    fn default() -> Self {
        ReachabilityOptions {
            state_limit: 200_000,
            use_invariant_precheck: true,
            invariant_bounds: InvariantBounds::default(),
        }
    }
}

/// 可达时的证据：合法变迁名序列与逐步标识。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ReachedCertificate {
    /// 按发射顺序的变迁名。
    pub transition_sequence: Vec<String>,
    /// transitions.len()+1 个标识：M0, M1, …, Mk=目标。
    pub marking_trace: Vec<Vec<i64>>,
    pub path_length: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct InvariantObstruction {
    /// 形成否决的非负 P 不变量权向量。
    pub weights: Vec<i64>,
    pub initial_weighted_sum: i128,
    pub target_weighted_sum: i128,
    pub residual_max_abs: i128,
}

#[derive(Debug, Clone, Serialize)]
pub struct SearchProgress {
    pub visited_states: u64,
    pub frontier_size: usize,
    pub state_space_upper_bound: u128,
}

#[derive(Debug, Clone, Serialize)]
pub struct ReachabilityResult {
    pub decision: ReachabilityDecision,
    pub target: Vec<i64>,
    pub basis: String,
    pub certificate: Option<ReachedCertificate>,
    /// 被某个 P 不变量候选的加权守恒否决时的见证（Reachable 时为 None）。
    pub invariant_obstruction: Option<InvariantObstruction>,
    pub states_visited: u64,
    pub state_space_upper_bound: u128,
    pub state_limit: u64,
    pub progress: Vec<SearchProgress>,
    /// 完备性边界声明，响应里原样返回。
    pub scope: String,
}

/// 理论状态空间上界 Π(c_p+1)，饱和到 u128，不截断、不假装有界。
pub fn state_space_upper_bound(net: &Net) -> u128 {
    net.places.iter().fold(1u128, |acc, p| {
        acc.saturating_mul(u128::try_from(p.capacity).unwrap_or(0).saturating_add(1))
    })
}

/// 执行可达性分析。
pub fn analyze_reachability(
    net: &Net,
    initial: &Marking,
    target: &Marking,
    options: &ReachabilityOptions,
) -> ReachabilityResult {
    let upper = state_space_upper_bound(net);
    let scope = "finite state space induced by explicit per-place capacities; exhaustiveness \
                 holds only within this capacity model and must not be read as a complete \
                 reachability decision for the corresponding unbounded net"
        .to_string();

    if initial == target {
        return ReachabilityResult {
            decision: ReachabilityDecision::Reachable,
            target: target.0.clone(),
            basis: "target_equals_initial".to_string(),
            certificate: Some(ReachedCertificate {
                transition_sequence: Vec::new(),
                marking_trace: vec![initial.0.clone()],
                path_length: 0,
            }),
            invariant_obstruction: None,
            states_visited: 1,
            state_space_upper_bound: upper,
            state_limit: options.state_limit,
            progress: Vec::new(),
            scope,
        };
    }

    // 1) P 不变量加权守恒快速否决（仅使用候选；候选枚举被截断时不得作为完备否决，
    //    但单个候选本身是合法见证，只要它确实是守恒律且初/目标加权和不等）。
    if options.use_invariant_precheck {
        let report = compute_invariants(net, options.invariant_bounds);
        for inv in &report.candidates {
            let residual = super::invariants::conservation_residual(net, &inv.weights);
            let max_res = residual.iter().map(|r| r.unsigned_abs() as i128).max().unwrap_or(0);
            let ws0 = weighted_sum(&inv.weights, &initial.0);
            let wst = weighted_sum(&inv.weights, &target.0);
            if max_res == 0 && ws0 != wst {
                info!(
                    candidates = report.candidates.len(),
                    truncated = report.candidates_truncated,
                    weighted_initial = ws0,
                    weighted_target = wst,
                    "reachability rejected by P-invariant conservation witness"
                );
                return ReachabilityResult {
                    decision: ReachabilityDecision::Unreachable,
                    target: target.0.clone(),
                    basis: "p_invariant_conservation_witness".to_string(),
                    certificate: None,
                    invariant_obstruction: Some(InvariantObstruction {
                        weights: inv.weights.clone(),
                        initial_weighted_sum: ws0,
                        target_weighted_sum: wst,
                        residual_max_abs: 0,
                    }),
                    states_visited: 0,
                    state_space_upper_bound: upper,
                    state_limit: options.state_limit,
                    progress: Vec::new(),
                    scope,
                };
            }
        }
    }

    // 2) 正向 BFS 穷尽（容量）状态空间。
    info!(
        state_space_upper_bound = upper,
        state_limit = options.state_limit,
        "start reachability BFS"
    );

    let mut visited: HashMap<Vec<i64>, (Vec<i64>, usize)> = HashMap::new();
    // key=当前标识，value=(父标识, 使用的变迁索引)；初标识的父指向自己。
    visited.insert(initial.0.clone(), (initial.0.clone(), usize::MAX));
    let mut queue: VecDeque<Vec<i64>> = VecDeque::new();
    queue.push_back(initial.0.clone());

    let mut progress = Vec::new();
    let mut found: Option<Vec<i64>> = None;
    let mut visited_count: u64 = 1;
    let mut exhausted = false;

    while let Some(cur) = queue.pop_front() {
        let cur_marking = Marking(cur.clone());
        debug!(visited = visited_count, frontier = queue.len(), "BFS expand");

        for (ti, t) in net.transitions.iter().enumerate() {
            let _ = t;
            match fire::fire(net, &cur_marking, ti) {
                Ok(next) => {
                    if !visited.contains_key(&next.0) {
                        visited_count += 1;
                        visited.insert(next.0.clone(), (cur.clone(), ti));

                        if &next == target {
                            found = Some(next.0.clone());
                            break;
                        }
                        queue.push_back(next.0.clone());

                        if visited_count.is_power_of_two() && progress.len() < 32 {
                            progress.push(SearchProgress {
                                visited_states: visited_count,
                                frontier_size: queue.len(),
                                state_space_upper_bound: upper,
                            });
                        }
                        if visited_count >= options.state_limit {
                            break;
                        }
                    }
                }
                // 禁用变迁（令牌不足/容量溢出）：正常跳过，绝不截断令牌。
                Err(super::fire::FireError::Blocked(reasons)) => {
                    debug!(
                        transition = net.transitions[ti].name,
                        deficits = reasons.insufficient_inputs.len(),
                        overflows = reasons.capacity_overflows.len(),
                        "transition blocked"
                    );
                }
                Err(super::fire::FireError::Invalid(e)) => {
                    // 内部一致性错误：不能吞掉后返回成功。
                    panic!("kernel invariant violation during BFS: {e}");
                }
            }
        }

        if found.is_some() {
            break;
        }
        if visited_count >= options.state_limit {
            break;
        }
        if queue.is_empty() {
            exhausted = true;
        }
    }

    if let Some(goal) = found {
        // 回溯路径。
        let mut seq_rev: Vec<usize> = Vec::new();
        let mut trace_rev: Vec<Vec<i64>> = Vec::new();
        let mut node = goal;
        trace_rev.push(node.clone());
        while node != *initial.0 {
            let (parent, ti) = &visited[&node];
            seq_rev.push(*ti);
            node = parent.clone();
            trace_rev.push(node.clone());
        }
        seq_rev.reverse();
        trace_rev.reverse();
        let cert = ReachedCertificate {
            transition_sequence: seq_rev
                .iter()
                .map(|&ti| net.transitions[ti].name.clone())
                .collect(),
            marking_trace: trace_rev,
            path_length: seq_rev.len(),
        };
        info!(path_length = cert.path_length, visited_count, "target reachable");
        ReachabilityResult {
            decision: ReachabilityDecision::Reachable,
            target: target.0.clone(),
            basis: "bfs_explicit_path".to_string(),
            certificate: Some(cert),
            invariant_obstruction: None,
            states_visited: visited_count,
            state_space_upper_bound: upper,
            state_limit: options.state_limit,
            progress,
            scope,
        }
    } else if exhausted {
        info!(visited_count, "capacity state space exhausted: target unreachable");
        ReachabilityResult {
            decision: ReachabilityDecision::Unreachable,
            target: target.0.clone(),
            basis: "capacity_state_space_exhausted".to_string(),
            certificate: None,
            invariant_obstruction: None,
            states_visited: visited_count,
            state_space_upper_bound: upper,
            state_limit: options.state_limit,
            progress,
            scope,
        }
    } else {
        info!(
            visited_count,
            state_limit = options.state_limit,
            "state limit reached before exhausting space: inconclusive"
        );
        ReachabilityResult {
            decision: ReachabilityDecision::Inconclusive,
            target: target.0.clone(),
            basis: "state_limit_reached".to_string(),
            certificate: None,
            invariant_obstruction: None,
            states_visited: visited_count,
            state_space_upper_bound: upper,
            state_limit: options.state_limit,
            progress,
            scope,
        }
    }
}

/// 从候选列表中选出一个非负 P 不变量（供 API/测试复用）。
pub fn first_invariant(candidates: &[PInvariant]) -> Option<&PInvariant> {
    candidates.first()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::kernel::model::{ArcExpr, Marking, Net, Place, Transition};

    #[test]
    fn finds_shortest_path_and_keeps_capacity() {
        // 线性：p0 -> p1 -> p2，容量各 1。
        let net = Net {
            places: vec![
                Place { name: "p0".into(), capacity: 1 },
                Place { name: "p1".into(), capacity: 1 },
                Place { name: "p2".into(), capacity: 1 },
            ],
            transitions: vec![
                Transition {
                    name: "t1".into(),
                    inputs: vec![ArcExpr { place: 0, weight: 1 }],
                    outputs: vec![ArcExpr { place: 1, weight: 1 }],
                },
                Transition {
                    name: "t2".into(),
                    inputs: vec![ArcExpr { place: 1, weight: 1 }],
                    outputs: vec![ArcExpr { place: 2, weight: 1 }],
                },
            ],
        };
        let m0 = Marking(vec![1, 0, 0]);
        let goal = Marking(vec![0, 0, 1]);
        let r = analyze_reachability(&net, &m0, &goal, &ReachabilityOptions::default());
        assert_eq!(r.decision, ReachabilityDecision::Reachable);
        let cert = r.certificate.clone().unwrap();
        assert_eq!(cert.transition_sequence, vec!["t1", "t2"]);
        assert_eq!(cert.marking_trace.first().unwrap(), &vec![1, 0, 0]);
        assert_eq!(cert.marking_trace.last().unwrap(), &vec![0, 0, 1]);
        // 路径上的每个标识都在容量内。
        for m in &cert.marking_trace {
            for (i, tok) in m.iter().enumerate() {
                assert!(0 <= *tok && *tok <= net.places[i].capacity);
            }
        }
    }

    #[test]
    fn capacity_exhaustion_proves_unreachable_within_model() {
        // 单库所容量 1，自环净 +1：从 0 只能到 1；2 不可达（容量穷尽）。
        let net = Net {
            places: vec![Place { name: "p".into(), capacity: 1 }],
            transitions: vec![Transition {
                name: "inc".into(),
                inputs: vec![],
                outputs: vec![ArcExpr { place: 0, weight: 1 }],
            }],
        };
        let m0 = Marking(vec![0]);
        let goal = Marking(vec![2]);
        let r = analyze_reachability(&net, &m0, &goal, &ReachabilityOptions::default());
        assert_eq!(r.decision, ReachabilityDecision::Unreachable);
        assert_eq!(r.basis, "capacity_state_space_exhausted");
        assert!(r.scope.contains("capacity model"));
    }

    #[test]
    fn state_limit_yields_inconclusive_not_unreachable() {
        // 容量 100 的单库所自增网：用极小 state_limit 截断。
        let net = Net {
            places: vec![Place { name: "p".into(), capacity: 100 }],
            transitions: vec![Transition {
                name: "inc".into(),
                inputs: vec![],
                outputs: vec![ArcExpr { place: 0, weight: 1 }],
            }],
        };
        let opts = ReachabilityOptions {
            state_limit: 3,
            ..Default::default()
        };
        let r = analyze_reachability(&net, &Marking(vec![0]), &Marking(vec![50]), &opts);
        assert_eq!(r.decision, ReachabilityDecision::Inconclusive);
        assert_eq!(r.basis, "state_limit_reached");
        assert!(r.certificate.is_none());
    }

    #[test]
    fn invariant_precheck_rejects_without_search() {
        // p0 -> p1（守恒）：初 [1,0]，目标 [1,1] 加权和 1 vs 2，不变量否决且不搜索。
        let net = Net {
            places: vec![
                Place { name: "p0".into(), capacity: 3 },
                Place { name: "p1".into(), capacity: 3 },
            ],
            transitions: vec![Transition {
                name: "t".into(),
                inputs: vec![ArcExpr { place: 0, weight: 1 }],
                outputs: vec![ArcExpr { place: 1, weight: 1 }],
            }],
        };
        let r = analyze_reachability(
            &net,
            &Marking(vec![1, 0]),
            &Marking(vec![1, 1]),
            &ReachabilityOptions::default(),
        );
        assert_eq!(r.decision, ReachabilityDecision::Unreachable);
        assert_eq!(r.basis, "p_invariant_conservation_witness");
        assert_eq!(r.states_visited, 0);
        let obs = r.invariant_obstruction.unwrap();
        assert_eq!(obs.weights, vec![1, 1]);
        assert_ne!(obs.initial_weighted_sum, obs.target_weighted_sum);
    }
}
