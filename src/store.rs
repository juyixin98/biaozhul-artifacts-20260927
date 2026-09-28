//! 状态层：命名约束集合与原子批次提交。
//!
//! 并发模型：内部使用 `std::sync::RwLock`，计算量较大的操作由接口层放入
//! `spawn_blocking` 执行。**一次批次的“校验 -> 配额 -> 求解 -> 提交”全程持有同一把写锁**：
//! 任一步失败（含使集合不可满足、整数溢出）都在写状态前返回，
//! 因而同一批请求期间，其他读请求只能看到“整批之前”或“整批之后”的集合，
//! 绝不会读到半更新集合。
//!
//! 不变量：已提交集合恒为可满足（不可满足的批次/重置被拒绝）。

use std::collections::BTreeMap;
use std::sync::{Arc, RwLock};

use crate::evidence::verify_cycle;
use crate::model::{BatchResponse, ConflictDto, ConstraintInput, RemoveResponse};
use crate::solver::{build_graph, solve_trace, KernelOutcome};

/// 状态层错误（无 HTTP 语义；由接口层映射为 `ApiError`）。
#[derive(Debug, Clone)]
pub enum StoreError {
    /// 输入语言问题。
    Input(String),
    /// 与当前状态冲突（重名、批次使集合不可满足）；不可满足时携带证据。
    Conflict {
        message: String,
        evidence: Option<ConflictDto>,
    },
    /// 超过变量/约束配额。
    Resource(String),
    /// 整数加法溢出等无法完成的计算。
    Computation(String),
    /// 命名资源不存在。
    NotFound(String),
    /// 内核/状态不变量被破坏。
    Internal(String),
}

#[derive(Debug, Clone)]
struct Inner {
    version: u64,
    constraints: BTreeMap<String, ConstraintInput>,
}

/// 共享约束集合（接口层以 `Arc<ConstraintStore>` 持有）。
#[derive(Clone)]
pub struct ConstraintStore {
    inner: Arc<RwLock<Inner>>,
}

impl Default for ConstraintStore {
    fn default() -> Self {
        Self::new()
    }
}

impl ConstraintStore {
    pub fn new() -> Self {
        ConstraintStore {
            inner: Arc::new(RwLock::new(Inner {
                version: 0,
                constraints: BTreeMap::new(),
            })),
        }
    }

    /// 当前集合快照（读锁在拷贝完成后立即释放）。
    pub fn snapshot(&self) -> (u64, Vec<ConstraintInput>) {
        let guard = self.inner.read().expect("store lock poisoned");
        (guard.version, guard.constraints.values().cloned().collect())
    }

    /// 当前版本号。
    pub fn version(&self) -> u64 {
        self.inner.read().expect("store lock poisoned").version
    }

    /// 原子追加一批约束。成功才改变状态；不可满足时返回负环证据。
    pub fn apply_batch(&self, batch: Vec<ConstraintInput>) -> Result<BatchResponse, StoreError> {
        // 1) 输入校验（锁外即可，但必须在批次内重名检查之前）。
        for c in &batch {
            if let Err(msg) = crate::model::validate_constraint(c) {
                return Err(StoreError::Input(msg));
            }
        }
        // 2) 批次内部的重名属于请求自身矛盾（input_error），
        //    与已存在集合重名属于状态冲突（state_conflict）。
        let mut in_batch = std::collections::HashSet::new();
        for c in &batch {
            if !in_batch.insert(c.name.as_str()) {
                return Err(StoreError::Input(format!(
                    "constraint name appears twice within the same batch: {}",
                    c.name
                )));
            }
        }

        // 3) 持写锁完成与现有集合的合并检查、求解与提交。
        let mut state = self.inner.write().expect("store lock poisoned");

        for c in &batch {
            if state.constraints.contains_key(&c.name) {
                return Err(StoreError::Conflict {
                    message: format!("constraint name already exists in set: {}", c.name),
                    evidence: None,
                });
            }
        }

        let combined = self.rows_with(&state.constraints, &batch);
        let outcome = solve_rows(&combined)?;

        match outcome {
            KernelOutcome::Unsat { .. } => {
                let evidence = evidence_from_outcome(&outcome, &combined)?;
                // 未触碰 state：锁释放后集合保持原状。
                Err(StoreError::Conflict {
                    message: "batch rejected: constraint set would be unsatisfiable".to_string(),
                    evidence: Some(evidence),
                })
            }
            KernelOutcome::Feasible { .. } => {
                let added = batch.len();
                for c in batch {
                    state.constraints.insert(c.name.clone(), c);
                }
                state.version += 1;
                let response = BatchResponse {
                    version: state.version,
                    constraint_count: state.constraints.len(),
                    variable_count: count_variables(&state.constraints),
                };
                tracing::info!(
                    target: "diff_constraints::commit",
                    version = response.version,
                    added,
                    total_constraints = response.constraint_count,
                    variables = response.variable_count,
                    verdict = "committed_feasible",
                    "atomic batch committed"
                );
                Ok(response)
            }
        }
    }

    /// 用新集合原子替换当前集合（reset）。新集合不可满足则拒绝，状态不变。
    pub fn replace_all(&self, next: Vec<ConstraintInput>) -> Result<BatchResponse, StoreError> {
        for c in &next {
            if let Err(msg) = crate::model::validate_constraint(c) {
                return Err(StoreError::Input(msg));
            }
        }
        let mut seen = std::collections::HashSet::new();
        for c in &next {
            if !seen.insert(c.name.as_str()) {
                return Err(StoreError::Input(format!(
                    "constraint name appears twice within reset set: {}",
                    c.name
                )));
            }
        }

        let mut state = self.inner.write().expect("store lock poisoned");
        let rows: Vec<_> = next
            .iter()
            .map(|c| (c.name.clone(), c.x.clone(), c.y.clone(), c.c))
            .collect();
        let outcome = solve_rows(&rows)?;

        if let KernelOutcome::Unsat { .. } = outcome {
            let evidence = evidence_from_outcome(&outcome, &rows)?;
            return Err(StoreError::Conflict {
                message: "reset rejected: new constraint set is unsatisfiable".to_string(),
                evidence: Some(evidence),
            });
        }

        let new_map: BTreeMap<_, _> = next.into_iter().map(|c| (c.name.clone(), c)).collect();
        let constraint_count = new_map.len();
        let variable_count = count_variables(&new_map);
        state.constraints = new_map;
        state.version += 1;
        Ok(BatchResponse {
            version: state.version,
            constraint_count,
            variable_count,
        })
    }

    /// 删除一条命名约束。删除只会放松集合，不会破坏可行性。
    pub fn remove(&self, name: &str) -> Result<RemoveResponse, StoreError> {
        let mut state = self.inner.write().expect("store lock poisoned");
        if state.constraints.remove(name).is_none() {
            return Err(StoreError::NotFound(format!(
                "constraint not found: {name}"
            )));
        }
        state.version += 1;
        Ok(RemoveResponse {
            version: state.version,
            removed: name.to_string(),
            constraint_count: state.constraints.len(),
        })
    }

    /// 对当前集合重新求解并给出变量名 -> 赋值。已提交集合按不变量必可行。
    pub fn solution(&self) -> Result<(u64, BTreeMap<String, i64>), StoreError> {
        let state = self.inner.read().expect("store lock poisoned");
        let version = state.version;
        let rows = self.rows_with(&state.constraints, &[]);
        let graph = build_graph(&rows).map_err(map_solver_error)?;
        match solve_trace(&graph).map_err(map_solver_error)? {
            KernelOutcome::Feasible { assignment, trace } => {
                tracing::debug!(
                    target: "diff_constraints::solve",
                    version, variables = graph.variable_count(),
                    constraints = graph.edge_count(), passes = trace.len(),
                    verdict = "feasible",
                    "solution request"
                );
                let mut map = BTreeMap::new();
                for (idx, value) in assignment.into_iter().enumerate() {
                    map.insert(graph.var_names[idx].clone(), value);
                }
                Ok((version, map))
            }
            KernelOutcome::Unsat { .. } => Err(StoreError::Internal(
                "invariant violated: committed constraint set is unsatisfiable".to_string(),
            )),
        }
    }

    fn rows_with(
        &self,
        existing: &BTreeMap<String, ConstraintInput>,
        extra: &[ConstraintInput],
    ) -> Vec<(String, String, String, i64)> {
        let mut rows: Vec<_> = existing
            .values()
            .map(|c| (c.name.clone(), c.x.clone(), c.y.clone(), c.c))
            .collect();
        rows.extend(
            extra
                .iter()
                .map(|c| (c.name.clone(), c.x.clone(), c.y.clone(), c.c)),
        );
        rows
    }
}

fn count_variables(map: &BTreeMap<String, ConstraintInput>) -> usize {
    let mut vars = std::collections::HashSet::new();
    for c in map.values() {
        vars.insert(c.x.as_str());
        vars.insert(c.y.as_str());
    }
    vars.len()
}

/// 编译并求解一组行，把内核错误映射为状态层错误（状态尚未修改）。
fn solve_rows(rows: &[(String, String, String, i64)]) -> Result<KernelOutcome, StoreError> {
    let graph = build_graph(rows).map_err(map_solver_error)?;
    let outcome = solve_trace(&graph).map_err(map_solver_error)?;
    match &outcome {
        KernelOutcome::Feasible { trace, .. } => {
            let last_relaxations = trace
                .iter()
                .rev()
                .find(|p| !p.detection)
                .map(|p| p.relaxations);
            tracing::debug!(
                target: "diff_constraints::solve",
                variables = graph.variable_count(),
                constraints = graph.edge_count(),
                passes = trace.len(),
                last_relaxations,
                verdict = "feasible",
                "candidate set solved"
            );
        }
        KernelOutcome::Unsat {
            cycle_edges,
            total_cost,
            detection_pass,
        } => {
            tracing::warn!(
                target: "diff_constraints::solve",
                variables = graph.variable_count(),
                constraints = graph.edge_count(),
                detection_pass,
                cycle_len = cycle_edges.len(),
                total_cost,
                verdict = "unsat_negative_cycle",
                "negative cycle found; batch will be rejected"
            );
        }
    }
    Ok(outcome)
}

/// 把内核负环结果映射为对外 DTO，并调用独立的证据验证模块复核严格负性。
fn evidence_from_outcome(
    outcome: &KernelOutcome,
    rows: &[(String, String, String, i64)],
) -> Result<ConflictDto, StoreError> {
    let (cycle_edges, total_cost) = match outcome {
        KernelOutcome::Unsat {
            cycle_edges,
            total_cost,
            ..
        } => (cycle_edges, *total_cost),
        _ => unreachable!("evidence_from_outcome called on feasible outcome"),
    };

    let graph = build_graph(rows).map_err(map_solver_error)?;
    let mut names_in_order: Vec<String> = Vec::with_capacity(cycle_edges.len());
    for &ei in cycle_edges {
        let source = graph.edges[ei].source;
        names_in_order.push(rows[source].0.clone());
    }

    let by_name: BTreeMap<_, _> = rows
        .iter()
        .map(|(name, x, y, c)| {
            (
                name.clone(),
                ConstraintInput {
                    name: name.clone(),
                    x: x.clone(),
                    y: y.clone(),
                    c: *c,
                },
            )
        })
        .collect();

    // 独立复核：不经过内核 Bellman-Ford，直接按输入语言重算边与费用。
    match verify_cycle(&names_in_order, &by_name) {
        Ok(Ok(dto)) => {
            debug_assert_eq!(dto.total_cost, total_cost);
            Ok(dto)
        }
        Ok(Err(reason)) => Err(StoreError::Internal(format!(
            "kernel-produced cycle failed independent verification: {reason}"
        ))),
        Err(crate::evidence::VerifyError::ArithmeticOverflow(msg)) => {
            Err(StoreError::Computation(msg))
        }
        Err(other) => Err(StoreError::Internal(format!(
            "kernel-produced cycle failed independent verification: {other:?}"
        ))),
    }
}

fn map_solver_error(err: crate::solver::SolverError) -> StoreError {
    use crate::solver::SolverError::*;
    match err {
        TooManyVariables { limit, got } => StoreError::Resource(format!(
            "variable count {got} exceeds service limit {limit}"
        )),
        TooManyConstraints { limit, got } => StoreError::Resource(format!(
            "constraint count {got} exceeds service limit {limit}"
        )),
        ArithmeticOverflow { where_ } => StoreError::Computation(format!(
            "integer addition overflow ({where_}); request rejected without wrapping"
        )),
        InvariantViolation(msg) => StoreError::Internal(msg),
    }
}
