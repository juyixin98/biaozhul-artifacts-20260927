//! HTTP handlers and the router.
//!
//! Routes:
//!
//! | Method | Path | Purpose |
//! |--------|------|---------|
//! | GET  | `/healthz` | liveness |
//! | POST | `/managers` | create a manager with a fixed variable order |
//! | GET  | `/managers/:id` | introspect a manager |
//! | POST | `/managers/:id/build` | parse + build an expression |
//! | POST | `/managers/:id/not` | complement a function |
//! | POST | `/managers/:id/apply` | binary connective |
//! | POST | `/managers/:id/restrict` | variable cofactor |
//! | POST | `/managers/:id/sat` | one satisfying assignment |
//! | POST | `/managers/:id/gc` | root-preserving garbage collection |
//! | POST | `/equiv` | equivalence query (own fresh managers) |

use std::sync::Arc;

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::json;

use crate::diag::{new_request_id, redact, Diag, Outcome};
use crate::kernel::Op;
use crate::verify::{check_equivalence, Decision, EquivQuery};

use super::dto::{
    ApplyReq, BuildReq, CreateManagerReq, CreateManagerResp, Envelope, EquivReq, EquivResp,
    FlattenReport, GcReq, GcResp, ManagerInfoResp, NodeResp, NotReq, RestrictReq, SatResp,
};
use super::state::{parse_expr, parse_op, ApiError, ApiResult, AppState};

fn envelope(diag: Diag) -> Envelope {
    Envelope { diag }
}

pub fn router(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/managers", post(create_manager))
        .route("/managers/:id", get(manager_info))
        .route("/managers/:id/build", post(build_expr))
        .route("/managers/:id/not", post(not_node))
        .route("/managers/:id/apply", post(apply_op))
        .route("/managers/:id/restrict", post(restrict))
        .route("/managers/:id/sat", post(sat))
        .route("/managers/:id/gc", post(gc))
        .route("/equiv", post(equiv))
        .with_state(state)
}

async fn healthz() -> impl IntoResponse {
    (StatusCode::OK, Json(json!({"status": "ok"})))
}

async fn create_manager(
    State(state): State<Arc<AppState>>,
    Json(req): Json<CreateManagerReq>,
) -> ApiResult<impl IntoResponse> {
    let request_id = new_request_id();
    let manager = crate::kernel::BddManager::new(req.order.clone())
        .map_err(|e| ApiError::from(e).tag(&request_id))?;
    let manager_id = manager.id();
    let order = manager.order().to_vec();
    state.managers.lock().unwrap().insert(manager_id, manager);
    tracing::info!(request_id = %request_id, manager_id, vars = order.len(), "manager created");
    Ok((
        StatusCode::CREATED,
        Json(CreateManagerResp {
            manager_id,
            order,
            envelope: envelope(Diag::ok(
                &request_id,
                "manager created with fixed variable order",
            )),
        }),
    ))
}

fn with_manager<T>(
    state: &AppState,
    request_id: &str,
    id: u64,
    f: impl FnOnce(&mut crate::kernel::BddManager) -> ApiResult<T>,
) -> ApiResult<T> {
    let mut table = state.managers.lock().unwrap();
    let mgr = table
        .get_mut(&id)
        .ok_or_else(|| ApiError::not_found(request_id, format!("no manager with id {id}")))?;
    f(mgr)
}

async fn manager_info(
    State(state): State<Arc<AppState>>,
    Path(id): Path<u64>,
) -> ApiResult<impl IntoResponse> {
    let request_id = new_request_id();
    let table = state.managers.lock().unwrap();
    let mgr = table
        .get(&id)
        .ok_or_else(|| ApiError::not_found(&request_id, format!("no manager with id {id}")))?;
    Ok(Json(ManagerInfoResp {
        manager_id: mgr.id(),
        epoch: mgr.epoch(),
        order: mgr.order().to_vec(),
        node_count: mgr.node_count(),
        internal_count: mgr.internal_count(),
        envelope: envelope(Diag::ok(&request_id, "manager introspection")),
    }))
}

async fn build_expr(
    State(state): State<Arc<AppState>>,
    Path(id): Path<u64>,
    Json(req): Json<BuildReq>,
) -> ApiResult<impl IntoResponse> {
    let request_id = new_request_id();
    let expr = parse_expr(&request_id, &req.expr)?;
    with_manager(&state, &request_id, id, |mgr| {
        let node = mgr
            .build(&expr)
            .map_err(|e| ApiError::from(e).tag(&request_id))?;
        let internal = mgr.internal_count();
        tracing::info!(
            request_id = %request_id,
            manager_id = id,
            internal_nodes = internal,
            "expression built and reduced"
        );
        Ok(Json(NodeResp {
            node_ref: node,
            internal_nodes: internal,
            envelope: envelope(Diag::ok(
                &request_id,
                "expression reduced to a canonical ROBDD node",
            )),
        }))
    })
}

async fn not_node(
    State(state): State<Arc<AppState>>,
    Path(id): Path<u64>,
    Json(req): Json<NotReq>,
) -> ApiResult<impl IntoResponse> {
    let request_id = new_request_id();
    with_manager(&state, &request_id, id, |mgr| {
        let node = mgr
            .not(&req.node_ref)
            .map_err(|e| ApiError::from(e).tag(&request_id))?;
        Ok(Json(NodeResp {
            node_ref: node,
            internal_nodes: mgr.internal_count(),
            envelope: envelope(Diag::ok(
                &request_id,
                "complement is a free edge-parity flip",
            )),
        }))
    })
}

async fn apply_op(
    State(state): State<Arc<AppState>>,
    Path(id): Path<u64>,
    Json(req): Json<ApplyReq>,
) -> ApiResult<impl IntoResponse> {
    let request_id = new_request_id();
    let op = parse_op(&request_id, &req.op)?;
    with_manager(&state, &request_id, id, |mgr| {
        let rhs = req.rhs.as_ref().ok_or_else(|| {
            ApiError::bad_request(
                &request_id,
                "missing-operand",
                "apply needs an `rhs` reference",
            )
        })?;
        let node = mgr
            .apply_op(op, &req.lhs, rhs)
            .map_err(|e| ApiError::from(e).tag(&request_id))?;
        Ok(Json(NodeResp {
            node_ref: node,
            internal_nodes: mgr.internal_count(),
            envelope: envelope(Diag::ok(
                &request_id,
                "apply reduced via Shannon expansion and the unique table",
            )),
        }))
    })
}

async fn restrict(
    State(state): State<Arc<AppState>>,
    Path(id): Path<u64>,
    Json(req): Json<RestrictReq>,
) -> ApiResult<impl IntoResponse> {
    let request_id = new_request_id();
    with_manager(&state, &request_id, id, |mgr| {
        let node = mgr
            .restrict(&req.var, req.value, &req.node_ref)
            .map_err(|e| ApiError::from(e).tag(&request_id))?;
        Ok(Json(NodeResp {
            node_ref: node,
            internal_nodes: mgr.internal_count(),
            envelope: envelope(Diag::ok(
                &request_id,
                "variable restricted to a constant and the result re-reduced",
            )),
        }))
    })
}

async fn sat(
    State(state): State<Arc<AppState>>,
    Path(id): Path<u64>,
    Json(req): Json<NotReq>,
) -> ApiResult<impl IntoResponse> {
    let request_id = new_request_id();
    with_manager(&state, &request_id, id, |mgr| {
        let witness = mgr
            .sat_witness(&req.node_ref)
            .map_err(|e| ApiError::from(e).tag(&request_id))?;
        let satisfiable = witness.is_some();
        Ok(Json(SatResp {
            satisfiable,
            witness,
            envelope: envelope(Diag::ok(
                &request_id,
                if satisfiable {
                    "satisfiable: a root-to-true path yields the witness"
                } else {
                    "unsatisfiable: the canonical edge is the false terminal"
                },
            )),
        }))
    })
}

async fn gc(
    State(state): State<Arc<AppState>>,
    Path(id): Path<u64>,
    Json(req): Json<GcReq>,
) -> ApiResult<impl IntoResponse> {
    let request_id = new_request_id();
    with_manager(&state, &request_id, id, |mgr| {
        let root_refs: Vec<&crate::kernel::NodeRef> = req.roots.iter().collect();
        let (report, new_roots) = mgr
            .gc(&root_refs)
            .map_err(|e| ApiError::from(e).tag(&request_id))?;
        tracing::info!(
            request_id = %request_id,
            manager_id = id,
            collected = report.collected,
            nodes_after = report.nodes_after,
            "garbage collection preserved all declared roots"
        );
        Ok(Json(GcResp {
            collected: report.collected,
            nodes_before: report.nodes_before,
            nodes_after: report.nodes_after,
            epoch_before: report.epoch_before,
            epoch_after: report.epoch_after,
            roots: new_roots,
            envelope: envelope(Diag::ok(
                &request_id,
                "mark-sweep-compact kept every reachable root and retired the rest",
            )),
        }))
    })
}

async fn equiv(
    State(s): State<Arc<AppState>>,
    Json(req): Json<EquivReq>,
) -> ApiResult<impl IntoResponse> {
    let request_id = new_request_id();
    let lhs_expr = parse_expr(&request_id, &req.lhs.expr)?;
    let rhs_expr = parse_expr(&request_id, &req.rhs.expr)?;

    // Default identity mapping on shared variable names.
    let mapping = if req.mapping.is_empty() {
        lhs_expr
            .variables()
            .into_iter()
            .filter(|n| req.rhs.order.contains(n))
            .map(|n| (n.clone(), n))
            .collect()
    } else {
        req.mapping.clone()
    };

    let limit = req.max_assignments.unwrap_or(s.config.max_truth_table_rows);

    // The client label is sensitive: only its redaction appears in the log.
    let sensitive = json!({
        "client_label": req.client_label.as_deref().map(redact),
    });

    let report = check_equivalence(&EquivQuery {
        lhs: &lhs_expr,
        lhs_order: &req.lhs.order,
        rhs: &rhs_expr,
        rhs_order: &req.rhs.order,
        mapping,
        max_assignments: limit,
    })
    .map_err(|e| ApiError::from(e).tag(&request_id))?;

    let (outcome, code) = match report.decision {
        Decision::Accepted => (Outcome::Accepted, "equivalent"),
        Decision::Rejected => (Outcome::Rejected, "not-equivalent"),
        Decision::Inconclusive => (Outcome::Inconclusive, "inconclusive"),
    };
    let mut diag = Diag {
        request_id: request_id.clone(),
        outcome,
        code: code.into(),
        reason: report.reason.clone(),
        state: json!({
            "lhs_internal_nodes": report.lhs_internal_nodes,
            "rhs_internal_nodes": report.rhs_internal_nodes,
            "assignments_checked": report.assignments_checked,
            "oracle_checked": report.oracle_checked,
            "limit": limit,
        }),
        sensitive,
    };
    diag.request_id = request_id.clone();

    tracing::info!(
        request_id = %request_id,
        decision = report.decision.as_str(),
        assignments_checked = report.assignments_checked,
        "equivalence query decided: {}", report.reason
    );

    let status = match report.decision {
        Decision::Accepted | Decision::Inconclusive => StatusCode::OK,
        Decision::Rejected => StatusCode::UNPROCESSABLE_ENTITY,
    };

    Ok((
        status,
        Json(EquivResp {
            decision: report.decision.as_str().into(),
            equivalent: report.equivalent,
            report: FlattenReport::from(&report),
            envelope: envelope(diag),
        }),
    ))
}

// Keep `Op` reachable for future kernel-driven endpoints without warnings.
#[allow(unused_imports)]
use Op as _Op;
