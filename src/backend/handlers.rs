//! Axum 处理器。
//!
//! 所有 JSON 响应统一信封：
//! - 成功 `{ "request_id", "data" }`；
//! - 失败 `{ "request_id", "error": { "code", "message" } }`（`diag` 携带
//!   脱敏后的关键状态）。
//!
//! 结论分类：
//! - **接受（200）**：运算完成，或等价判定给出 equivalent / not_equivalent；
//! - **拒绝（4xx）**：请求不合法（解析、未知变量、跨管理器边、映射非双射等）；
//! - **无法判定（200, verdict=inconclusive）**：请求合法但证据不足（变量过多）。

use std::collections::HashMap;

use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::{Extension, Json};
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::core::apply::Op;
use crate::core::gc::GcReport;
use crate::core::{BddError, Edge};
use crate::lang::parser;
use crate::lang::Expr;
use crate::verify::{equiv_exprs, EdgeEquivReport, EquivResult, IdentityMapping};

use super::diag::{Ctx, Decision, RequestId};
use super::state::{decode_edge_token, encode_edge, AppState};

// ---------------------------------------------------------------------------
// 请求 / 响应结构
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct CreateManagerReq {
    pub variable_order: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct CreateManagerResp {
    pub manager_id: u64,
    pub seq: u64,
    pub variable_order: Vec<String>,
}

#[derive(Debug, Deserialize)]
pub struct BuildReq {
    /// 文本语法表达式（与 `expr_json` 二选一）。
    pub expr: Option<String>,
    /// JSON 语法树表达式。
    pub expr_json: Option<Expr>,
    /// 注册为命名根（GC 时保留）；缺省不注册。
    pub root_name: Option<String>,
    /// 本次请求包含敏感数据：日志/错误中对表达式只输出指纹。
    #[serde(default)]
    pub sensitive: bool,
}

#[derive(Debug, Serialize)]
pub struct EdgeResp {
    pub edge: String,
    pub value: Option<bool>,
    pub top_var: Option<String>,
    pub live_nodes: usize,
}

#[derive(Debug, Deserialize)]
pub struct ApplyReq {
    pub op: Op,
    pub a: String,
    pub b: String,
    pub root_name: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct RestrictReq {
    pub edge: String,
    /// 变量名 -> 布尔值。
    pub values: HashMap<String, bool>,
    pub root_name: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct GcResp {
    pub report: GcReport,
    pub roots: Vec<String>,
}

#[derive(Debug, Deserialize)]
pub struct ListRootsQuery {
    #[serde(default)]
    pub include_edge: bool,
}

#[derive(Debug, Serialize)]
pub struct RootInfo {
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub edge: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct EvaluateReq {
    pub edge: String,
    pub assignment: HashMap<String, bool>,
}

/// 一次性等价查询（表达式不需要提前建管理器）。
#[derive(Debug, Deserialize)]
pub struct EquivalenceReq {
    pub left_expr: String,
    pub right_expr: String,
    /// 身份映射：左名 -> 右名；同名变量自动配对。
    #[serde(default)]
    pub mapping: HashMap<String, String>,
    /// 两端变量序（缺省按各自变量首次出现顺序）。
    pub left_order: Option<Vec<String>>,
    pub right_order: Option<Vec<String>>,
    #[serde(default)]
    pub sensitive: bool,
}

#[derive(Debug, Serialize)]
pub struct EquivalenceResp {
    pub verdict: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<EquivResult>,
    /// 内核规范边结论与独立真值表结论的交叉比对（同管理器重建后）。
    pub cross_check: EdgeEquivReport,
    pub left_nodes: usize,
    pub right_nodes: usize,
}

// ---------------------------------------------------------------------------
// 错误到 HTTP 状态码的映射
// ---------------------------------------------------------------------------

fn error_status(err: &BddError) -> StatusCode {
    match err {
        BddError::ParseFailed { .. }
        | BddError::UnknownVariable(_)
        | BddError::DuplicateVariable(_)
        | BddError::MalformedEdgeToken(_)
        | BddError::MappingRejected(_) => StatusCode::UNPROCESSABLE_ENTITY, // 422
        BddError::ForeignManager { .. } | BddError::NotInSameManager { .. } => {
            StatusCode::UNPROCESSABLE_ENTITY // 422：跨管理器混用属于请求错误
        }
        BddError::ReclaimedNode { .. } => StatusCode::GONE, // 410
        BddError::UnknownManager(_) => StatusCode::NOT_FOUND, // 404
        BddError::TruthTableTooLarge { .. } => StatusCode::OK, // 处理器转为 inconclusive 200
    }
}

fn error_code(err: &BddError) -> &'static str {
    match err {
        BddError::ParseFailed { .. } => "parse_failed",
        BddError::UnknownVariable(_) => "unknown_variable",
        BddError::DuplicateVariable(_) => "duplicate_variable",
        BddError::ForeignManager { .. } => "foreign_manager",
        BddError::ReclaimedNode { .. } => "reclaimed_node",
        BddError::NotInSameManager { .. } => "not_same_manager",
        BddError::TruthTableTooLarge { .. } => "truth_table_too_large",
        BddError::MappingRejected(_) => "mapping_rejected",
        BddError::UnknownManager(_) => "unknown_manager",
        BddError::MalformedEdgeToken(_) => "malformed_edge_token",
    }
}

/// 处理器错误：携带请求上下文，`IntoResponse` 时统一记日志、脱敏。
pub(crate) struct ApiError {
    ctx: Ctx,
    status: StatusCode,
    code: &'static str,
    message: String,
    expr_fp: Option<String>,
}

impl ApiError {
    fn from_bdd(
        ctx: &Ctx,
        err: BddError,
        mut state: serde_json::Map<String, serde_json::Value>,
    ) -> Self {
        let status = error_status(&err);
        let code = error_code(&err);
        // 敏感模式下错误消息不回显任何用户表达式片段。
        let message = if ctx.sensitive
            && matches!(
                err,
                BddError::UnknownVariable(_) | BddError::ParseFailed { .. }
            ) {
            format!("{code} (details redacted; see expr fingerprint)")
        } else {
            err.to_string()
        };
        state.insert("error_code".into(), json!(code));
        ctx.diag(Decision::Rejected, code).with_state(state).log();
        ApiError {
            ctx: ctx.clone(),
            status,
            code,
            message,
            expr_fp: None,
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = json!({
            "request_id": self.ctx.rid.as_str(),
            "error": {
                "code": self.code,
                "message": self.message,
            },
            "diag": {
                "expr_fp": self.expr_fp,
            },
        });
        (self.status, Json(body)).into_response()
    }
}

type ApiResult<T> = Result<T, ApiError>;

fn ok_response<T: Serialize>(rid: &RequestId, data: T) -> Response {
    (
        StatusCode::OK,
        Json(json!({ "request_id": rid.as_str(), "data": data })),
    )
        .into_response()
}

fn ctx_from(rid: &RequestId, sensitive: bool) -> Ctx {
    Ctx::new(rid.clone(), sensitive)
}

fn parse_expr_input(req: &BuildReq, ctx: &Ctx) -> Result<Expr, ApiError> {
    match (&req.expr, &req.expr_json) {
        (Some(_), Some(_)) => Err(ApiError::from_bdd(
            ctx,
            BddError::ParseFailed {
                pos: 0,
                message: "provide only one of expr or expr_json".into(),
            },
            serde_json::Map::new(),
        )),
        (Some(text), None) => parser::parse(text).map_err(|e| {
            let mut st = serde_json::Map::new();
            st.insert("parse_pos".into(), json!(e.pos));
            let mut err = ApiError::from_bdd(
                ctx,
                BddError::ParseFailed {
                    pos: e.pos,
                    message: e.message.clone(),
                },
                st,
            );
            err.expr_fp = Some(ctx.redact_expr(text).unwrap_or_default());
            err
        }),
        (None, Some(e)) => Ok(e.clone()),
        (None, None) => Err(ApiError::from_bdd(
            ctx,
            BddError::ParseFailed {
                pos: 0,
                message: "missing expr or expr_json".into(),
            },
            serde_json::Map::new(),
        )),
    }
}

fn edge_summary(state: &AppState, manager_id: u64, edge: Edge) -> EdgeResp {
    let (value, top_var, live_nodes) = state
        .with_manager(manager_id, |m| {
            let top = m
                .top_var(edge)?
                .and_then(|v| m.variable_order().get(v.0 as usize).cloned());
            Ok((edge.as_value(), top, m.live_node_count()))
        })
        .unwrap_or((None, None, 0));
    EdgeResp {
        edge: encode_edge(edge),
        value,
        top_var,
        live_nodes,
    }
}

// ---------------------------------------------------------------------------
// 处理器
// ---------------------------------------------------------------------------

pub(crate) async fn health(
    State(state): State<AppState>,
    Extension(rid): Extension<RequestId>,
) -> Response {
    let mut st = serde_json::Map::new();
    st.insert("managers".into(), json!(state.manager_count()));
    ok_response(
        &rid,
        json!({ "status": "ok", "managers": state.manager_count() }),
    )
}

pub(crate) async fn create_manager(
    State(state): State<AppState>,
    Extension(rid): Extension<RequestId>,
    Json(req): Json<CreateManagerReq>,
) -> ApiResult<Response> {
    let ctx = ctx_from(&rid, false);
    let mut vars = req.variable_order.clone();
    // 拒绝重复变量名（内核也会查，这里先给出带上下文的诊断）。
    let mut seen = std::collections::HashSet::new();
    for v in &vars {
        if !seen.insert(v.clone()) {
            let mut st = serde_json::Map::new();
            st.insert("variable".into(), json!(v));
            return Err(ApiError::from_bdd(
                &ctx,
                BddError::DuplicateVariable(v.clone()),
                st,
            ));
        }
    }
    let (seq, mid) = state
        .create_manager(std::mem::take(&mut vars))
        .map_err(|e| ApiError::from_bdd(&ctx, e, serde_json::Map::new()))?;

    let mut st = serde_json::Map::new();
    st.insert("manager_id".into(), json!(mid.0));
    st.insert("variables".into(), json!(req.variable_order.len()));
    ctx.diag(Decision::Accepted, "manager_created")
        .with_state(st)
        .log();

    Ok(ok_response(
        &rid,
        CreateManagerResp {
            manager_id: mid.0,
            seq,
            variable_order: req.variable_order,
        },
    ))
}

pub(crate) async fn build(
    State(app): State<AppState>,
    Extension(rid): Extension<RequestId>,
    Path(manager_id): Path<u64>,
    Json(req): Json<BuildReq>,
) -> ApiResult<Response> {
    let ctx = ctx_from(&rid, req.sensitive);
    let expr = parse_expr_input(&req, &ctx)?;
    let expr_raw = req
        .expr
        .clone()
        .unwrap_or_else(|| "<json-expr>".to_string());
    let fp = ctx.redact_expr(&expr_raw);

    let result = app.with_manager(manager_id, |m| {
        let edge = m.build(&expr)?;
        if let Some(name) = &req.root_name {
            m.add_root(name, edge)?;
        }
        let summary = EdgeResp {
            edge: String::new(),
            value: edge.as_value(),
            top_var: m
                .top_var(edge)?
                .and_then(|v| m.variable_order().get(v.0 as usize).cloned()),
            live_nodes: m.live_node_count(),
        };
        Ok((edge, summary))
    });
    let (edge, mut summary) = result.map_err(|e| {
        let mut st = serde_json::Map::new();
        st.insert("manager_id".into(), json!(manager_id));
        let mut err = ApiError::from_bdd(&ctx, e, st);
        err.expr_fp = fp.clone();
        err
    })?;
    summary.edge = encode_edge(edge);

    let mut st = serde_json::Map::new();
    st.insert("manager_id".into(), json!(manager_id));
    st.insert("live_nodes".into(), json!(summary.live_nodes));
    st.insert("is_constant".into(), json!(summary.value.is_some()));
    ctx.diag(Decision::Accepted, "built")
        .with_state(st)
        .with_expr_fp(fp)
        .log();
    Ok(ok_response(&rid, summary))
}

pub(crate) async fn apply(
    State(app): State<AppState>,
    Extension(rid): Extension<RequestId>,
    Path(manager_id): Path<u64>,
    Json(req): Json<ApplyReq>,
) -> ApiResult<Response> {
    let ctx = ctx_from(&rid, false);
    let a = decode_edge_token(&app, &req.a)
        .map_err(|e| ApiError::from_bdd(&ctx, e, edge_diag_state(manager_id, &req.a)))?;
    let b = decode_edge_token(&app, &req.b)
        .map_err(|e| ApiError::from_bdd(&ctx, e, edge_diag_state(manager_id, &req.b)))?;

    let result = app.with_manager(manager_id, |m| {
        let edge = m.apply(req.op, a, b)?;
        if let Some(name) = &req.root_name {
            m.add_root(name, edge)?;
        }
        Ok((edge, m.live_node_count(), edge.as_value()))
    });
    let (edge, live_nodes, value) = result.map_err(|e| {
        let mut st = edge_diag_state(manager_id, &req.a);
        st.insert("op".into(), json!(req.op.to_string()));
        ApiError::from_bdd(&ctx, e, st)
    })?;

    let summary = EdgeResp {
        edge: encode_edge(edge),
        value,
        top_var: edge_summary(&app, manager_id, edge).top_var,
        live_nodes,
    };
    let mut st = serde_json::Map::new();
    st.insert("op".into(), json!(req.op.to_string()));
    st.insert("live_nodes".into(), json!(live_nodes));
    ctx.diag(Decision::Accepted, "applied").with_state(st).log();
    Ok(ok_response(&rid, summary))
}

pub(crate) async fn restrict(
    State(app): State<AppState>,
    Extension(rid): Extension<RequestId>,
    Path(manager_id): Path<u64>,
    Json(req): Json<RestrictReq>,
) -> ApiResult<Response> {
    let ctx = ctx_from(&rid, false);
    let edge = decode_edge_token(&app, &req.edge)
        .map_err(|e| ApiError::from_bdd(&ctx, e, edge_diag_state(manager_id, &req.edge)))?;

    let result = app.with_manager(manager_id, |m| {
        let out = m.restrict(edge, &req.values)?;
        if let Some(name) = &req.root_name {
            m.add_root(name, out)?;
        }
        Ok((out, m.live_node_count(), out.as_value()))
    });
    let (out, live_nodes, value) = result.map_err(|e| {
        let mut st = edge_diag_state(manager_id, &req.edge);
        st.insert("restrictions".into(), json!(req.values.len()));
        ApiError::from_bdd(&ctx, e, st)
    })?;

    let summary = EdgeResp {
        edge: encode_edge(out),
        value,
        top_var: edge_summary(&app, manager_id, out).top_var,
        live_nodes,
    };
    let mut st = serde_json::Map::new();
    st.insert("restrictions".into(), json!(req.values.len()));
    st.insert("live_nodes".into(), json!(live_nodes));
    ctx.diag(Decision::Accepted, "restricted")
        .with_state(st)
        .log();
    Ok(ok_response(&rid, summary))
}

pub(crate) async fn evaluate(
    State(app): State<AppState>,
    Extension(rid): Extension<RequestId>,
    Path(manager_id): Path<u64>,
    Json(req): Json<EvaluateReq>,
) -> ApiResult<Response> {
    let ctx = ctx_from(&rid, false);
    let edge = decode_edge_token(&app, &req.edge)
        .map_err(|e| ApiError::from_bdd(&ctx, e, edge_diag_state(manager_id, &req.edge)))?;
    let value = app
        .with_manager(manager_id, |m| {
            let mut ordered = vec![false; m.variable_order().len()];
            for (name, bit) in &req.assignment {
                let vid = m
                    .var_id(name)
                    .ok_or_else(|| BddError::UnknownVariable(name.clone()))?;
                ordered[vid.0 as usize] = *bit;
            }
            m.evaluate(edge, &ordered)
        })
        .map_err(|e| ApiError::from_bdd(&ctx, e, serde_json::Map::new()))?;
    Ok(ok_response(&rid, json!({ "value": value })))
}

pub(crate) async fn gc(
    State(app): State<AppState>,
    Extension(rid): Extension<RequestId>,
    Path(manager_id): Path<u64>,
) -> ApiResult<Response> {
    let ctx = ctx_from(&rid, false);
    let (report, roots) = app
        .with_manager(manager_id, |m| {
            let report = m.gc();
            let roots = m.root_names();
            Ok((report, roots))
        })
        .map_err(|e| ApiError::from_bdd(&ctx, e, serde_json::Map::new()))?;

    let mut st = serde_json::Map::new();
    st.insert("before".into(), json!(report.before_nodes));
    st.insert("after".into(), json!(report.after_nodes));
    st.insert("swept".into(), json!(report.swept));
    ctx.diag(Decision::Accepted, "gc_complete")
        .with_state(st)
        .log();
    Ok(ok_response(&rid, GcResp { report, roots }))
}

pub(crate) async fn list_roots(
    State(app): State<AppState>,
    Extension(rid): Extension<RequestId>,
    Path(manager_id): Path<u64>,
    Query(q): Query<ListRootsQuery>,
) -> ApiResult<Response> {
    let ctx = ctx_from(&rid, false);
    let infos = app
        .with_manager(manager_id, |m| {
            let mut out = Vec::new();
            for name in m.root_names() {
                let edge = m.root(&name);
                out.push(RootInfo {
                    name,
                    edge: if q.include_edge {
                        edge.map(encode_edge)
                    } else {
                        None
                    },
                });
            }
            Ok(out)
        })
        .map_err(|e| ApiError::from_bdd(&ctx, e, serde_json::Map::new()))?;
    Ok(ok_response(&rid, json!({ "roots": infos })))
}

pub(crate) async fn delete_manager(
    State(app): State<AppState>,
    Extension(rid): Extension<RequestId>,
    Path(manager_id): Path<u64>,
) -> ApiResult<Response> {
    let ctx = ctx_from(&rid, false);
    if app.delete_manager(manager_id) {
        Ok(ok_response(&rid, json!({ "deleted": manager_id })))
    } else {
        Err(ApiError::from_bdd(
            &ctx,
            BddError::UnknownManager(manager_id),
            serde_json::Map::new(),
        ))
    }
}

/// 一次性等价查询：独立真值表判定 + 同管理器重建的内核交叉比对。
pub(crate) async fn equivalence(
    State(app): State<AppState>,
    Extension(rid): Extension<RequestId>,
    Extension(cap): Extension<VarCap>,
    Json(req): Json<EquivalenceReq>,
) -> ApiResult<Response> {
    let ctx = ctx_from(&rid, req.sensitive);
    let fp_l = ctx.redact_expr(&req.left_expr);
    let _fp_r = ctx.redact_expr(&req.right_expr);

    let parse_side = |text: &str| -> Result<Expr, ApiError> {
        parser::parse(text).map_err(|e| {
            let mut st = serde_json::Map::new();
            st.insert("parse_pos".into(), json!(e.pos));
            let mut err = ApiError::from_bdd(
                &ctx,
                BddError::ParseFailed {
                    pos: e.pos,
                    message: e.message.clone(),
                },
                st,
            );
            err.expr_fp = Some(ctx.redact_expr(text).unwrap_or_default());
            err
        })
    };
    let left = parse_side(&req.left_expr)?;
    let right = parse_side(&req.right_expr)?;

    let mapping = IdentityMapping::new(req.mapping.clone());

    // 独立真值表判定（参考结论）。
    let result = equiv_exprs(&left, &right, &mapping, cap.0).map_err(|e| {
        let mut st = serde_json::Map::new();
        st.insert("mapping_pairs".into(), json!(req.mapping.len()));
        let mut err = ApiError::from_bdd(&ctx, e, st);
        err.expr_fp = fp_l.clone();
        err
    })?;

    // 内核交叉比对：在一个临时管理器内把两边都规范化建出来。
    // 变量序：左侧身份按字母序；右侧按映射重命名进同一身份空间。
    let cross = cross_check_kernel(&app, &left, &right, &mapping, cap.0, &ctx)?;

    // 结论一致性检查：内核规范边相等 <=> 真值表等价。
    let (verdict, reason) = match &result {
        EquivResult::Equivalent { .. } if cross.canonical_equal => ("equivalent", None),
        EquivResult::NotEquivalent { .. } if !cross.canonical_equal => ("not_equivalent", None),
        EquivResult::Equivalent { .. } => (
            "inconclusive",
            Some("oracle says equivalent but kernel canonical edges differ".to_string()),
        ),
        EquivResult::NotEquivalent { .. } => (
            "inconclusive",
            Some("oracle found a witness but kernel canonical edges are equal".to_string()),
        ),
    };
    if verdict == "inconclusive" {
        let mut st = serde_json::Map::new();
        st.insert("canonical_equal".into(), json!(cross.canonical_equal));
        st.insert(
            "oracle_equivalent".into(),
            json!(cross.truth_table_equivalent),
        );
        ctx.diag(Decision::Inconclusive, "kernel_oracle_disagreement")
            .with_state(st)
            .log();
    } else {
        let mut st = serde_json::Map::new();
        st.insert(
            "assignments_checked".into(),
            json!(cross.assignments_checked),
        );
        st.insert("left_nodes".into(), json!(cross.left_nodes));
        ctx.diag(Decision::Accepted, format!("equiv_{verdict}"))
            .with_state(st)
            .with_expr_fp(fp_l)
            .log();
    }

    // 左/右节点数：内核交叉比对时已统计。
    let (left_nodes, right_nodes) = (cross.left_nodes, cross.right_nodes);
    let cross_report = EdgeEquivReport {
        canonical_equal: cross.canonical_equal,
        truth_table_equivalent: cross.truth_table_equivalent,
        assignments_checked: cross.assignments_checked,
        witness: cross.witness,
    };

    Ok(ok_response(
        &rid,
        EquivalenceResp {
            verdict: verdict.to_string(),
            reason: reason.map(|s| s.to_string()),
            result: Some(result),
            cross_check: cross_report,
            left_nodes,
            right_nodes,
        },
    ))
}

struct KernelCross {
    canonical_equal: bool,
    truth_table_equivalent: bool,
    assignments_checked: usize,
    witness: Option<crate::verify::Witness>,
    left_nodes: usize,
    right_nodes: usize,
}

/// 在专用临时管理器中构建两边并让内核/预言机交叉验证。
fn cross_check_kernel(
    _app: &AppState,
    left: &Expr,
    right: &Expr,
    mapping: &IdentityMapping,
    cap: usize,
    ctx: &Ctx,
) -> Result<KernelCross, ApiError> {
    let left_set: std::collections::HashSet<String> = left.vars().into_iter().collect();
    let right_set: std::collections::HashSet<String> = right.vars().into_iter().collect();
    let pairs = mapping.validate(&left_set, &right_set).map_err(|e| {
        ApiError::from_bdd(
            ctx,
            BddError::MappingRejected(e.to_string()),
            serde_json::Map::new(),
        )
    })?;
    let reverse: HashMap<String, String> = pairs.into_iter().map(|(l, r)| (r, l)).collect();
    let right_renamed = right.rename(&reverse);

    let mut identities: Vec<String> = left.vars();
    identities.extend(right_renamed.vars());
    identities.sort();
    identities.dedup();

    // 临时管理器不进入 HTTP 注册表（用完即弃），避免污染 manager_count。
    let mut m = crate::core::BddManager::new(&identities)
        .map_err(|e| ApiError::from_bdd(ctx, e, serde_json::Map::new()))?;
    let le = m
        .build(left)
        .map_err(|e| ApiError::from_bdd(ctx, e, serde_json::Map::new()))?;
    let left_nodes = m.live_node_count();
    let re = m
        .build(&right_renamed)
        .map_err(|e| ApiError::from_bdd(ctx, e, serde_json::Map::new()))?;
    // 右表达式构建后总存活节点数；由于共享唯一表，left_nodes/right_nodes
    // 是“构建该表达式时图中存活节点数”而非严格增量，仅作规模诊断证据。
    let right_nodes = m.live_node_count();

    let report = crate::verify::equiv_edges(&m, le, re, &identities, cap)
        .map_err(|e| ApiError::from_bdd(ctx, e, serde_json::Map::new()))?;
    Ok(KernelCross {
        canonical_equal: report.canonical_equal,
        truth_table_equivalent: report.truth_table_equivalent,
        assignments_checked: report.assignments_checked,
        witness: report.witness,
        left_nodes,
        right_nodes,
    })
}

/// 变量数上限的 Axum 状态。
#[derive(Clone, Copy)]
pub struct VarCap(pub usize);

fn edge_diag_state(manager_id: u64, token: &str) -> serde_json::Map<String, serde_json::Value> {
    let mut st = serde_json::Map::new();
    st.insert("manager_id".into(), json!(manager_id));
    // 令牌不是敏感表达式原文，但截断后放入以便关联。
    st.insert("edge_token_len".into(), json!(token.len()));
    st
}
