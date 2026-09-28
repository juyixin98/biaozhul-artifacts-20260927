//! 后端接口：axum 路由、run id 注入、错误到 HTTP 的映射。
//!
//! 所有响应带 `x-run-id` 头与日志中的 run id 一致；请求可自带 `x-run-id`
//! （用于重放），缺省时服务端生成递增编号 `r-000001`。
//!
//! 计算密集或持锁操作通过 [`tokio::task::spawn_blocking`] 执行，
//! 不在异步运行时线程上做 O(V·E) 的松弛循环。

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use axum::{
    body::Bytes,
    extract::{Path, Request, State},
    http::{header, HeaderMap, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{delete, get, post},
    Json, Router,
};

use crate::error::ApiError;
use crate::evidence::{verify_cycle, VerifyError};
use crate::model::{
    BatchRequest, BatchResponse, ConstraintInput, ConstraintsView, RemoveResponse, ResetRequest,
    SolutionResponse, SolveRequest, VerifyRequest, VerifyResponse,
};
use crate::solver::{build_graph, solve_trace, KernelOutcome};
use crate::store::{ConstraintStore, StoreError};

/// 应用共享状态。
pub struct AppState {
    pub store: ConstraintStore,
    run_counter: AtomicU64,
}

impl AppState {
    pub fn new(store: ConstraintStore) -> Arc<AppState> {
        Arc::new(AppState {
            store,
            run_counter: AtomicU64::new(0),
        })
    }

    fn next_run_id(&self) -> String {
        let n = self.run_counter.fetch_add(1, Ordering::Relaxed) + 1;
        format!("r-{n:06}")
    }
}

/// 构建完整路由器（测试与 main 共用）。
pub fn app(state: Arc<AppState>) -> Router {
    // fallback 必须在 layer 之前注册，否则兜底路由不会被 run-id 中间件包裹。
    Router::new()
        .route("/health", get(health))
        .route("/", get(index))
        .route("/v1/constraints", get(list_constraints))
        .route("/v1/constraints:batch", post(batch_add))
        .route("/v1/reset", post(reset))
        .route("/v1/constraints/{name}", delete(remove_constraint))
        .route("/v1/solution", get(solution))
        .route("/v1/solve", post(solve))
        .route("/v1/evidence/verify", post(verify))
        .fallback(fallback)
        .layer(middleware::from_fn_with_state(
            Arc::clone(&state),
            run_id_layer,
        ))
        .with_state(state)
}

/// 注入/回显 run id：请求自带 `x-run-id` 则沿用（重放），否则生成。
async fn run_id_layer(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    mut request: Request,
    next: Next,
) -> Response {
    let run_id = headers
        .get("x-run-id")
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .map(str::to_string)
        .unwrap_or_else(|| state.next_run_id());
    tracing::debug!(
        target: "diff_constraints::http",
        run_id = %run_id,
        method = %request.method(),
        path = %request.uri().path(),
        "request received"
    );
    request.extensions_mut().insert(run_id.clone());
    let mut response = next.run(request).await;
    if let Ok(value) = run_id.parse() {
        response.headers_mut().insert("x-run-id", value);
    }
    response
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok", "service": "diff-constraints-service" }))
}

async fn index() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "service": "diff-constraints-service",
        "form": "x - y <= c (integers)",
        "endpoints": [
            "GET  /health",
            "GET  /v1/constraints",
            "POST /v1/constraints:batch",
            "POST /v1/reset",
            "DELETE /v1/constraints/{name}",
            "GET  /v1/solution",
            "POST /v1/solve",
            "POST /v1/evidence/verify",
        ],
    }))
}

async fn list_constraints(
    State(state): State<Arc<AppState>>,
    axum::extract::Extension(run_id): axum::extract::Extension<String>,
) -> Json<ConstraintsView> {
    let (version, constraints) = run_blocking(run_id.clone(), {
        let store = state.store.clone();
        move || store.snapshot()
    })
    .await
    .expect("snapshot is infallible");
    Json(ConstraintsView {
        version,
        constraints,
    })
}

async fn batch_add(
    State(svc): State<Arc<AppState>>,
    axum::extract::Extension(run_id): axum::extract::Extension<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<BatchResponse>, ApiError> {
    let req: BatchRequest = parse_json(&body, &headers, &run_id)?;
    let store = svc.store.clone();
    let result = run_blocking(run_id.clone(), move || store.apply_batch(req.constraints)).await?;
    result.map(Json).map_err(|e| map_store_error(e, &run_id))
}

async fn reset(
    State(svc): State<Arc<AppState>>,
    axum::extract::Extension(run_id): axum::extract::Extension<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<BatchResponse>, ApiError> {
    let req: ResetRequest = parse_json(&body, &headers, &run_id)?;
    let store = svc.store.clone();
    let result = run_blocking(run_id.clone(), move || store.replace_all(req.constraints)).await?;
    result.map(Json).map_err(|e| map_store_error(e, &run_id))
}

async fn remove_constraint(
    State(svc): State<Arc<AppState>>,
    axum::extract::Extension(run_id): axum::extract::Extension<String>,
    Path(name): Path<String>,
) -> Result<Json<RemoveResponse>, ApiError> {
    let store = svc.store.clone();
    let result = run_blocking(run_id.clone(), move || store.remove(&name)).await?;
    result.map(Json).map_err(|e| map_store_error(e, &run_id))
}

async fn solution(
    State(svc): State<Arc<AppState>>,
    axum::extract::Extension(run_id): axum::extract::Extension<String>,
) -> Result<Json<SolutionResponse>, ApiError> {
    let store = svc.store.clone();
    let result = run_blocking(run_id.clone(), move || store.solution()).await?;
    let (version, assignment) = result.map_err(|e| map_store_error(e, &run_id))?;
    Ok(Json(SolutionResponse {
        version: Some(version),
        assignment,
    }))
}

/// 无状态求解：不触碰已存储集合，可行给赋值，不可行给 409 + 负环证据。
/// 无状态求解的闭包产出：可行映射或已复核的冲突证据。
enum StatelessResult {
    Feasible(BTreeMap<String, i64>),
    Unsat(crate::model::ConflictDto),
}

async fn solve(
    State(_svc): State<Arc<AppState>>,
    axum::extract::Extension(run_id): axum::extract::Extension<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<SolutionResponse>, ApiError> {
    let req: SolveRequest = parse_json(&body, &headers, &run_id)?;

    for c in &req.constraints {
        if let Err(msg) = crate::model::validate_constraint(c) {
            return Err(ApiError::input(msg, &run_id));
        }
    }
    let mut seen = std::collections::HashSet::new();
    for c in &req.constraints {
        if !seen.insert(c.name.as_str()) {
            return Err(ApiError::input(
                format!("duplicate constraint name within request: {}", c.name),
                &run_id,
            ));
        }
    }

    let constraints = req.constraints;
    let result = run_blocking(run_id.clone(), {
        let run_id = run_id.clone();
        move || -> Result<StatelessResult, ApiError> {
            let rows: Vec<_> = constraints
                .iter()
                .map(|c| (c.name.clone(), c.x.clone(), c.y.clone(), c.c))
                .collect();
            let graph = build_graph(&rows).map_err(|e| map_kernel_error(e, &run_id))?;
            let outcome = solve_trace(&graph).map_err(|e| map_kernel_error(e, &run_id))?;
            match outcome {
                KernelOutcome::Feasible { assignment, .. } => {
                    let mut map = BTreeMap::new();
                    for (idx, value) in assignment.into_iter().enumerate() {
                        map.insert(graph.var_names[idx].clone(), value);
                    }
                    Ok(StatelessResult::Feasible(map))
                }
                KernelOutcome::Unsat { cycle_edges, .. } => {
                    let names: Vec<String> = cycle_edges
                        .iter()
                        .map(|&ei| rows[graph.edges[ei].source].0.clone())
                        .collect();
                    let by_name: BTreeMap<_, _> = constraints
                        .iter()
                        .map(|c| (c.name.clone(), c.clone()))
                        .collect();
                    match verify_cycle(&names, &by_name) {
                        Ok(Ok(dto)) => Ok(StatelessResult::Unsat(dto)),
                        Ok(Err(reason)) => Err(ApiError::internal(
                            format!("kernel cycle failed independent verification: {reason}"),
                            &run_id,
                        )),
                        Err(VerifyError::ArithmeticOverflow(msg)) => {
                            Err(ApiError::computation(msg, &run_id))
                        }
                        Err(other) => Err(ApiError::internal(
                            format!("evidence verification failed: {other:?}"),
                            &run_id,
                        )),
                    }
                }
            }
        }
    })
    .await??;

    match result {
        StatelessResult::Feasible(assignment) => Ok(Json(SolutionResponse {
            version: None,
            assignment,
        })),
        StatelessResult::Unsat(evidence) => Err(ApiError::unsat(
            "constraint set is unsatisfiable",
            evidence,
            run_id,
        )),
    }
}

async fn verify(
    State(svc): State<Arc<AppState>>,
    axum::extract::Extension(run_id): axum::extract::Extension<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<VerifyResponse>, ApiError> {
    let req: VerifyRequest = parse_json(&body, &headers, &run_id)?;

    let by_name: BTreeMap<String, ConstraintInput> = match &req.constraints {
        Some(list) => {
            for c in list {
                if let Err(msg) = crate::model::validate_constraint(c) {
                    return Err(ApiError::input(msg, &run_id));
                }
            }
            let mut seen = std::collections::HashSet::new();
            for c in list {
                if !seen.insert(c.name.as_str()) {
                    return Err(ApiError::input(
                        format!("duplicate constraint name within request: {}", c.name),
                        &run_id,
                    ));
                }
            }
            list.iter().map(|c| (c.name.clone(), c.clone())).collect()
        }
        None => {
            let (_, snapshot) = run_blocking(run_id.clone(), {
                let store = svc.store.clone();
                move || store.snapshot()
            })
            .await
            .expect("snapshot is infallible");
            snapshot.into_iter().map(|c| (c.name.clone(), c)).collect()
        }
    };

    let ad_hoc = req.constraints.is_some();
    let result = run_blocking(run_id.clone(), move || verify_cycle(&req.cycle, &by_name)).await?;
    match result {
        Ok(Ok(evidence)) => Ok(Json(VerifyResponse {
            valid: true,
            reason: None,
            evidence: Some(evidence),
        })),
        Ok(Err(reason)) => Ok(Json(VerifyResponse {
            valid: false,
            reason: Some(reason),
            evidence: None,
        })),
        Err(VerifyError::Input(msg)) => Err(ApiError::input(msg, &run_id)),
        Err(VerifyError::UnknownConstraint(name)) => {
            if ad_hoc {
                Err(ApiError::input(
                    format!("cycle references constraint absent from supplied set: {name}"),
                    &run_id,
                ))
            } else {
                Err(ApiError::state(
                    format!("cycle references constraint absent from current set: {name}"),
                    &run_id,
                ))
            }
        }
        Err(VerifyError::ArithmeticOverflow(msg)) => Err(ApiError::computation(msg, &run_id)),
    }
}

// ---------- 辅助 ----------

/// 在阻塞线程池执行同步闭包；join 失败归入内部错误。
async fn run_blocking<T: Send + 'static>(
    run_id: String,
    f: impl FnOnce() -> T + Send + 'static,
) -> Result<T, ApiError> {
    tokio::task::spawn_blocking(f)
        .await
        .map_err(|e| ApiError::internal(format!("blocking task join failed: {e}"), &run_id))
}

fn parse_json<T: serde::de::DeserializeOwned>(
    body: &[u8],
    headers: &HeaderMap,
    run_id: &str,
) -> Result<T, ApiError> {
    if let Some(ct) = headers.get(header::CONTENT_TYPE) {
        if let Ok(s) = ct.to_str() {
            let mime = s.split(';').next().unwrap_or("").trim();
            if !mime.is_empty() && mime != "application/json" {
                return Err(ApiError::input(
                    format!("unsupported content-type: expected application/json, got {mime:?}"),
                    run_id,
                ));
            }
        }
    }
    serde_json::from_slice(body).map_err(|e| {
        // axum 的 2MB 体限制已在提取层之外；这里的解析失败统一为输入错误。
        ApiError::input(format!("malformed JSON request body: {e}"), run_id)
    })
}

fn map_store_error(err: StoreError, run_id: &str) -> ApiError {
    match err {
        StoreError::Input(msg) => ApiError::input(msg, run_id),
        StoreError::Conflict { message, evidence } => match evidence {
            Some(ev) => ApiError::unsat(message, ev, run_id),
            None => ApiError::state(message, run_id),
        },
        StoreError::Resource(msg) => ApiError::resource(msg, run_id),
        StoreError::Computation(msg) => ApiError::computation(msg, run_id),
        StoreError::NotFound(msg) => ApiError::not_found(msg, run_id),
        StoreError::Internal(msg) => ApiError::internal(msg, run_id),
    }
}

fn map_kernel_error(err: crate::solver::SolverError, run_id: &str) -> ApiError {
    use crate::solver::SolverError::*;
    match err {
        TooManyVariables { limit, got } => ApiError::resource(
            format!("variable count {got} exceeds service limit {limit}"),
            run_id,
        ),
        TooManyConstraints { limit, got } => ApiError::resource(
            format!("constraint count {got} exceeds service limit {limit}"),
            run_id,
        ),
        ArithmeticOverflow { where_ } => ApiError::computation(
            format!("integer addition overflow ({where_}); request rejected without wrapping"),
            run_id,
        ),
        InvariantViolation(msg) => ApiError::internal(msg, run_id),
    }
}

/// 兜底：未匹配路由的 404 也保持统一错误体（run-id 层已注入 run id）。
pub async fn fallback(
    axum::extract::Extension(run_id): axum::extract::Extension<String>,
) -> Response {
    (
        StatusCode::NOT_FOUND,
        Json(serde_json::json!({
            "error": {
                "category": "not_found",
                "message": "no such route; see GET / for the endpoint list",
                "run_id": run_id,
            }
        })),
    )
        .into_response()
}
