//! Solver kernel: Bellman–Ford feasibility for difference constraints.
//!
//! ## Algorithm
//!
//! Given the graph built by [`crate::graph`] (`y -> x` of weight `c` for
//! `x - y <= c`):
//!
//! 1. Initialize `dist[v] = 0` for **every** vertex. This is equivalent to
//!    adding a super-source connected to all vertices with weight 0, so all
//!    connected components are reached simultaneously.
//! 2. Relax every edge up to `n` passes.
//! 3. If a full pass relaxes nothing, the shortest-path inequalities hold and
//!    `dist` itself is a feasible integer assignment.
//! 4. If an edge can still be relaxed on pass `n`, a negative cycle exists.
//!    The predecessor chain from the relaxed vertex is walked `n` steps to
//!    land on a cycle vertex, then walked around to recover the cycle.
//!
//! ## Overflow policy
//!
//! Every edge relaxation uses [`i64::checked_add`]. If a path weight would
//! leave the `i64` range the caller asked us to work in, the solve fails with
//! [`ErrorKind::ComputationFailed`] instead of wrapping — a wrapped distance
//! could turn an infeasible system into a bogus witness.
//!
//! [`ErrorKind::ComputationFailed`]: crate::error::ErrorKind::ComputationFailed

use serde::Serialize;

use crate::error::{ServiceError, ServiceResult};
use crate::graph::{Edge, Graph};

/// Vertex/edge counts at or above which the solver refuses to run. The store
/// enforces the same limits on mutation; the kernel re-checks them so it is
/// safe to call directly from tests.
pub const MAX_VERTICES: usize = 4096;
pub const MAX_EDGES: usize = 8192;

/// Detailed per-relaxation data is only retained for small instances, so a
/// trace can never grow as O(n·m) memory.
pub const TRACE_DETAIL_LIMIT: usize = 64;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct RelaxRecord {
    pub edge: String,
    pub from: String,
    pub to: String,
    pub old_dist: i64,
    pub new_dist: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct PassDetail {
    /// Distance vector immediately after the pass.
    pub distances: Vec<i64>,
    /// Relaxations performed during the pass, in edge order.
    pub relaxed_edges: Vec<RelaxRecord>,
}

#[derive(Debug, Clone, Serialize)]
pub struct PassTrace {
    /// 1-based pass number.
    pub pass: usize,
    pub relaxations: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<PassDetail>,
}

/// Replayable trace of the run: one entry per executed pass plus the final
/// distance vector and a human-readable explanation of the decision.
#[derive(Debug, Clone, Serialize)]
pub struct SolveTrace {
    pub passes: Vec<PassTrace>,
    pub final_distances: Vec<i64>,
    pub rationale: String,
}

/// Feasible result: the assignment satisfies every input constraint.
#[derive(Debug, Clone, Serialize)]
pub struct FeasibleWitness {
    /// `(variable, value)` pairs in graph vertex order.
    pub assignment: Vec<(String, i64)>,
    pub iterations: usize,
    pub trace: SolveTrace,
}

/// Infeasible result: the ordered negative cycle proving unsat.
#[derive(Debug, Clone, Serialize)]
pub struct NegativeCycle {
    /// Constraint ids in edge-walk order; walking the edges forms a closed
    /// loop. Only original constraint ids appear here.
    pub cycle_constraint_ids: Vec<String>,
    /// Variables visited by the cycle, starting and ending with the same
    /// vertex (length = edges + 1).
    pub cycle_vertices: Vec<String>,
    /// Strictly negative total weight.
    pub weight: i64,
    pub iterations: usize,
    pub trace: SolveTrace,
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum SolveOutcome {
    Feasible(FeasibleWitness),
    Infeasible(NegativeCycle),
}

struct Pred {
    vertex: Vec<Option<usize>>,
    edge: Vec<Option<usize>>,
}

/// Run Bellman–Ford over `graph`. See module docs for the contract.
pub fn bellman_ford(graph: &Graph) -> ServiceResult<SolveOutcome> {
    let n = graph.vertex_count();
    let m = graph.edges.len();
    if n > MAX_VERTICES {
        return Err(ServiceError::resource_exhausted(format!(
            "instance has {n} variables, limit is {MAX_VERTICES}"
        )));
    }
    if m > MAX_EDGES {
        return Err(ServiceError::resource_exhausted(format!(
            "instance has {m} constraints, limit is {MAX_EDGES}"
        )));
    }

    let mut dist = vec![0i64; n];
    let mut pred = Pred {
        vertex: vec![None; n],
        edge: vec![None; n],
    };
    let detailed = n <= TRACE_DETAIL_LIMIT;
    let mut passes: Vec<PassTrace> = Vec::new();

    // Empty system: trivially feasible, no passes needed.
    if n == 0 {
        return Ok(SolveOutcome::Feasible(FeasibleWitness {
            assignment: Vec::new(),
            iterations: 0,
            trace: SolveTrace {
                passes,
                final_distances: Vec::new(),
                rationale: "empty constraint set: no variables, vacuously feasible".into(),
            },
        }));
    }

    let mut last_relax: Option<(usize, usize, usize)> = None;

    for pass in 1..=n {
        let mut relaxations = 0usize;
        let mut records: Vec<RelaxRecord> = Vec::new();
        last_relax = None;

        for (ei, e) in graph.edges.iter().enumerate() {
            let candidate = relax_candidate(&dist, e)?;
            if candidate < dist[e.to] {
                if detailed {
                    records.push(RelaxRecord {
                        edge: e.constraint_id.clone(),
                        from: graph.variables[e.from].clone(),
                        to: graph.variables[e.to].clone(),
                        old_dist: dist[e.to],
                        new_dist: candidate,
                    });
                }
                dist[e.to] = candidate;
                pred.vertex[e.to] = Some(e.from);
                pred.edge[e.to] = Some(ei);
                relaxations += 1;
                last_relax = Some((e.from, e.to, ei));
            }
        }

        passes.push(PassTrace {
            pass,
            relaxations,
            detail: detailed.then(|| PassDetail {
                distances: dist.clone(),
                relaxed_edges: records,
            }),
        });

        if relaxations == 0 {
            let assignment = graph
                .variables
                .iter()
                .cloned()
                .zip(dist.iter().copied())
                .collect();
            return Ok(SolveOutcome::Feasible(FeasibleWitness {
                assignment,
                iterations: pass,
                trace: SolveTrace {
                    passes,
                    final_distances: dist,
                    rationale: format!(
                        "pass {pass} produced no relaxations; all shortest-path inequalities hold, \
                         so the distance vector is a feasible integer assignment"
                    ),
                },
            }));
        }
    }

    // An edge relaxed on pass n proves a reachable negative cycle.
    let cycle = extract_negative_cycle(graph, &pred, last_relax)?;
    let iterations = n;
    let rationale = format!(
        "at least one edge relaxed on every one of {n} passes; predecessor walk yields a closed walk \
         of strictly negative total weight {} < 0, which is an unsatisfiability proof",
        cycle.weight
    );
    Ok(SolveOutcome::Infeasible(NegativeCycle {
        cycle_constraint_ids: cycle.edge_ids,
        cycle_vertices: cycle.vertices,
        weight: cycle.weight,
        iterations,
        trace: SolveTrace {
            passes,
            final_distances: dist,
            rationale,
        },
    }))
}

/// One checked relaxation candidate `dist[from] + weight`.
fn relax_candidate(dist: &[i64], e: &Edge) -> ServiceResult<i64> {
    dist[e.from].checked_add(e.weight).ok_or_else(|| {
        ServiceError::computation_failed(format!(
            "relaxing edge '{}' ({} -> {}, weight {}): {} + {} overflows i64",
            e.constraint_id,
            e.from,
            e.to,
            e.weight,
            dist[e.from],
            e.weight
        ))
    })
}

struct ExtractedCycle {
    edge_ids: Vec<String>,
    vertices: Vec<String>,
    weight: i64,
}

/// Recover the negative cycle from predecessor pointers after the nth pass.
///
/// `last_relax = (u, v, ei)` is an edge `u -> v` that relaxed on pass n.
/// Walking predecessors `n` times from `v` is guaranteed to reach a vertex on
/// the cycle; walking from there until we return reconstructs it.
fn extract_negative_cycle(
    graph: &Graph,
    pred: &Pred,
    last_relax: Option<(usize, usize, usize)>,
) -> ServiceResult<ExtractedCycle> {
    let n = graph.vertex_count();
    let (_u, v, _detect_ei) = last_relax.ok_or_else(|| {
        ServiceError::computation_failed(
            "kernel invariant violated: nth pass claimed a relaxation but recorded none",
        )
    })?;

    // Walk n predecessor hops to reach a vertex guaranteed to be on the cycle.
    let mut on_cycle = v;
    for _ in 0..n {
        on_cycle = pred.vertex[on_cycle].ok_or_else(|| {
            ServiceError::computation_failed(
                "kernel invariant violated: predecessor chain ended while extracting negative cycle",
            )
        })?;
    }

    // Walk the cycle backwards collecting edge indices.
    let mut edge_indices_rev: Vec<usize> = Vec::new();
    let mut cur = on_cycle;
    loop {
        let ei = pred.edge[cur].ok_or_else(|| {
            ServiceError::computation_failed(
                "kernel invariant violated: missing predecessor edge on negative-cycle walk",
            )
        })?;
        let pv = pred.vertex[cur].unwrap();
        edge_indices_rev.push(ei);
        cur = pv;
        if cur == on_cycle {
            break;
        }
        if edge_indices_rev.len() > n {
            return Err(ServiceError::computation_failed(
                "kernel invariant violated: predecessor walk did not close into a cycle",
            ));
        }
    }
    edge_indices_rev.reverse();

    // Validate the forward chain: edges must connect and close at on_cycle.
    let mut edge_ids = Vec::with_capacity(edge_indices_rev.len());
    let mut vertices = vec![graph.variables[on_cycle].clone()];
    let mut weight: i64 = 0;
    let mut current = on_cycle;
    for ei in edge_indices_rev {
        let e = &graph.edges[ei];
        if e.from != current {
            return Err(ServiceError::computation_failed(format!(
                "kernel invariant violated: extracted edges do not chain at '{}'",
                graph.variables[current]
            )));
        }
        weight = weight.checked_add(e.weight).ok_or_else(|| {
            ServiceError::computation_failed(
                "summing the extracted negative cycle overflows i64",
            )
        })?;
        edge_ids.push(e.constraint_id.clone());
        current = e.to;
        vertices.push(graph.variables[current].clone());
    }
    if current != on_cycle {
        return Err(ServiceError::computation_failed(
            "kernel invariant violated: extracted walk does not close into a cycle",
        ));
    }
    if weight >= 0 {
        return Err(ServiceError::computation_failed(format!(
            "kernel invariant violated: extracted cycle has non-negative weight {weight}"
        )));
    }
    Ok(ExtractedCycle {
        edge_ids,
        vertices,
        weight,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The edge-limit branch is normally reached only via the store, which
    /// rejects duplicate ids. Construct a single-vertex graph with repeated
    /// self loops to exercise the kernel guard itself.
    #[test]
    fn edge_limit_is_enforced_by_the_kernel() {
        let g = Graph::self_loops_for_test(MAX_EDGES + 1);
        let err = bellman_ford(&g).expect_err("edge cap must reject");
        assert_eq!(err.kind, crate::error::ErrorKind::ResourceExhausted);
        assert!(err.message.contains(&MAX_EDGES.to_string()));
    }
}
