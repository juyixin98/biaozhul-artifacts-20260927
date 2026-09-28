//! Bellman-Ford 求解与负环取证。
//!
//! 初始化等价于加入隐式超源：所有 `dist[v] = 0`（超源到每个节点一条 0 权边），
//! 因此不连通分量中的负环同样可被发现，且超源边永远不会被实例化、不会进入证据。
//!
//! 每一次距离加法都使用 [`i64::checked_add`]：溢出即
//! [`SolverError::ArithmeticOverflow`](super::graph::SolverError::ArithmeticOverflow)，
//! 明确拒绝，不发生回绕。

use super::graph::{Graph, SolverError};
use super::KernelOutcome;

/// 一次松弛的关键中间状态（下标形式，变量名由外层映射）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RelaxSample {
    /// 发生松弛尝试的边下标。
    pub edge: usize,
    /// 边源节点。
    pub from: usize,
    /// 被更新/可更新的目标节点。
    pub vertex: usize,
    /// 松弛前 `dist[to]`。
    pub current_dist: i64,
    /// 候选值 `dist[from] + weight`（checked 得到，因此能记录即未溢出）。
    pub candidate_dist: i64,
}

/// 一轮扫描的记录，用于重放与日志。
#[derive(Debug, Clone)]
pub struct TracePass {
    /// 轮次，0-based；检测轮固定为 `n-1`。
    pub pass: usize,
    /// 是否为检测轮（第 n 轮，只检测不写回）。
    pub detection: bool,
    /// 本轮可松弛的边数。
    pub relaxations: usize,
    /// 本轮前若干次松弛尝试（最多 3 条），便于人工核对。
    pub samples: Vec<RelaxSample>,
}

const SAMPLES_PER_PASS: usize = 3;

/// 对编译后的图求解。
pub fn solve_trace(graph: &Graph) -> Result<KernelOutcome, SolverError> {
    let n = graph.variable_count();

    // 空集合：显然可行，唯一可行赋值为空。
    if n == 0 {
        return Ok(KernelOutcome::Feasible {
            assignment: Vec::new(),
            trace: Vec::new(),
        });
    }

    // 等价于显式超源：dist[v] = 0。前驱边下标；None 表示仍由隐式超源直达。
    let mut dist = vec![0_i64; n];
    let mut pred: Vec<Option<usize>> = vec![None; n];
    let mut trace: Vec<TracePass> = Vec::with_capacity(n);

    // 共 n 轮：前 n-1 轮常规松弛；第 n 轮（pass == n-1）为检测轮——
    // 一旦发现仍可松弛的边，按 CLRS 方式同样写回 dist/pred 后立即取环。
    for pass in 0..n {
        let detection = pass == n - 1;
        let mut relaxations = 0usize;
        let mut samples: Vec<RelaxSample> = Vec::new();
        let mut culprit: Option<usize> = None; // 检测轮中首个仍可松弛的目标节点

        for (ei, edge) in graph.edges.iter().enumerate() {
            let candidate = match dist[edge.from].checked_add(edge.weight) {
                Some(v) => v,
                None => {
                    return Err(SolverError::ArithmeticOverflow {
                        where_: format!(
                            "relaxation on edge {ei} (pass {pass}): dist[{}] + {}",
                            edge.from, edge.weight
                        ),
                    });
                }
            };
            if candidate < dist[edge.to] {
                relaxations += 1;
                if samples.len() < SAMPLES_PER_PASS {
                    samples.push(RelaxSample {
                        edge: ei,
                        from: edge.from,
                        vertex: edge.to,
                        current_dist: dist[edge.to],
                        candidate_dist: candidate,
                    });
                }
                dist[edge.to] = candidate;
                pred[edge.to] = Some(ei);
                if detection {
                    culprit = Some(edge.to);
                    break;
                }
            }
        }

        trace.push(TracePass {
            pass,
            detection,
            relaxations,
            samples,
        });

        if detection {
            if let Some(vertex) = culprit {
                let cycle_edges = extract_cycle(graph, &pred, vertex, n)?;
                let total_cost = cycle_cost_checked(graph, &cycle_edges)?;
                // 严格性由算法保证；若复核不严格则视为内核不变量被破坏。
                if total_cost >= 0 {
                    return Err(SolverError::InvariantViolation(format!(
                        "extracted cycle from vertex {vertex} is not strictly negative: cost={total_cost}"
                    )));
                }
                return Ok(KernelOutcome::Unsat {
                    cycle_edges,
                    total_cost,
                    detection_pass: pass,
                });
            }
        } else if relaxations == 0 {
            // 提前收敛：后续轮次不会再变化，检测轮必然无事发生。
            trace.push(TracePass {
                pass: pass + 1,
                detection: true,
                relaxations: 0,
                samples: Vec::new(),
            });
            break;
        }
    }

    Ok(KernelOutcome::Feasible {
        assignment: dist,
        trace,
    })
}

/// 沿前驱链从被松弛节点取出一个简单环（边下标，行走顺序）。
fn extract_cycle(
    graph: &Graph,
    pred: &[Option<usize>],
    vertex: usize,
    n: usize,
) -> Result<Vec<usize>, SolverError> {
    // 从顶点沿前驱走 n 步，保证落入某个环内（n-1 轮后仍可松弛的节点，
    // 其前驱链必含环）。
    let mut cur = vertex;
    for _ in 0..n {
        let ei = pred[cur].ok_or_else(|| {
            SolverError::InvariantViolation(format!(
                "predecessor chain ended at internal super-source edge from vertex {cur}"
            ))
        })?;
        cur = graph.edges[ei].from;
    }
    let start = cur;

    // 再从环内起点沿前驱收边，直到回到起点。
    let mut backward: Vec<usize> = Vec::new();
    loop {
        let ei = pred[cur].ok_or_else(|| {
            SolverError::InvariantViolation(
                "cycle extraction unexpectedly reached anonymous super-source edge".to_string(),
            )
        })?;
        backward.push(ei);
        cur = graph.edges[ei].from;
        if cur == start {
            break;
        }
        if backward.len() > n {
            return Err(SolverError::InvariantViolation(format!(
                "extracted walk longer than vertex count {n}; not a simple cycle"
            )));
        }
    }
    backward.reverse();
    Ok(backward)
}

/// 用 checked 加法复核环费用（拒绝溢出，且调用方据此断言严格为负）。
pub fn cycle_cost_checked(graph: &Graph, cycle_edges: &[usize]) -> Result<i64, SolverError> {
    let mut total = 0_i64;
    for &ei in cycle_edges {
        let w = graph.edges[ei].weight;
        total = total
            .checked_add(w)
            .ok_or_else(|| SolverError::ArithmeticOverflow {
                where_: format!("summing cycle cost at edge {ei} (weight {w})"),
            })?;
    }
    Ok(total)
}
