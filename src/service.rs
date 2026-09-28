//! 后端接口（Axum）：HTTP/JSON 边界、错误分类映射、带运行编号的诊断日志。

use axum::{
    body::Bytes,
    extract::State,
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::Serialize;
use std::panic::AssertUnwindSafe;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;
use tracing::{error, info, warn};
use uuid::Uuid;

use crate::error::{ApiError, ErrorCode, Result};
use crate::evidence;
use crate::input;
use crate::model::CheckRequestWire;
use crate::solver::{self, KernelVerdict, UnknownReason};

/// 服务端对单个请求体的大小上限。
const MAX_BODY_BYTES: usize = 64 * 1024 * 1024;

#[derive(Clone)]
struct AppState {
    seq: std::sync::Arc<AtomicU64>,
    started_at: std::time::SystemTime,
}

pub fn router() -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/check", post(check))
        .with_state(AppState {
            seq: std::sync::Arc::new(AtomicU64::new(0)),
            started_at: std::time::SystemTime::now(),
        })
}

async fn health(State(st): State<AppState>) -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "status": "ok",
        "service": "weak-trace-inclusion",
        "uptime_secs": st.started_at.elapsed().map(|d| d.as_secs()).unwrap_or(0),
    }))
}

#[derive(Debug, Serialize)]
struct ErrorBody {
    run_id: String,
    category: crate::error::ErrorCategory,
    code: ErrorCode,
    message: String,
    details: Vec<String>,
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let status =
            StatusCode::from_u16(self.category.http_status()).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
        // run_id 在错误路径下无法可靠取得（畸形 JSON 时），用 0000 标记。
        let body = ErrorBody {
            run_id: "00000000-0000-0000-0000-000000000000".to_owned(),
            category: self.category,
            code: self.code,
            message: self.message,
            details: self.details,
        };
        (status, Json(body)).into_response()
    }
}

fn error_with_run(mut err: ApiError, run_id: &str) -> Response {
    let status =
        StatusCode::from_u16(err.category.http_status()).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    let body = ErrorBody {
        run_id: run_id.to_owned(),
        category: err.category,
        code: err.code,
        message: std::mem::take(&mut err.message),
        details: std::mem::take(&mut err.details),
    };
    (status, Json(body)).into_response()
}

async fn check(State(st): State<AppState>, body: Bytes) -> Response {
    let seq = st.seq.fetch_add(1, Ordering::Relaxed);
    let started = Instant::now();
    let run_id = Uuid::new_v4().to_string();

    if body.len() > MAX_BODY_BYTES {
        let err = ApiError::new(
            ErrorCode::BodyTooLarge,
            format!(
                "请求体 {} 字节超过上限 {MAX_BODY_BYTES} 字节",
                body.len()
            ),
        );
        warn!(run_id = %run_id, seq = seq, code = ?err.code, "请求体过大");
        return error_with_run(err, &run_id);
    }

    // 第一阶段：JSON 语法层。
    let value: serde_json::Value = match serde_json::from_slice(body.as_ref()) {
        Ok(v) => v,
        Err(e) => {
            let err = ApiError::new(
                ErrorCode::MalformedJson,
                format!("请求体不是合法 JSON：{e}"),
            )
            .with_detail(format!("line={},column={}", e.line(), e.column()));
            warn!(run_id = %run_id, seq = seq, code = ?err.code, "JSON 解析失败");
            return error_with_run(err, &run_id);
        }
    };

    // 第二阶段：形状层（缺字段/类型错）。
    let request: CheckRequestWire = match serde_json::from_value(value) {
        Ok(r) => r,
        Err(e) => {
            let err = ApiError::new(
                ErrorCode::InvalidRequestShape,
                format!("请求结构不符合 CheckRequest 模式：{e}"),
            )
            .with_detail(e.to_string());
            warn!(run_id = %run_id, seq = seq, code = ?err.code, field = %e.to_string(), "请求结构错误");
            return error_with_run(err, &run_id);
        }
    };

    let client_run_id = request.run_id.clone();
    info!(
        run_id = %run_id,
        client_run_id = ?client_run_id,
        seq = seq,
        spec = %request.specification.name,
        implementation = %request.implementation.name,
        alphabet = request.observable_actions.len(),
        "开始弱迹包含检查"
    );

    // 第三阶段：语义校验与求解。panic 一律兜成 computation_failure，绝不泄漏为连接中断。
    let outcome = std::panic::catch_unwind(AssertUnwindSafe(|| {
        let (system, limits) = input::parse_request(request)?;
        let out = solver::solve(&system, &limits)
            .map_err(|e| ApiError::new(ErrorCode::SolverFailure, e.to_string()))?;
        Ok((system, out))
    }));

    let result: Result<(_, _)> = match outcome {
        Ok(r) => r,
        Err(p) => {
            let msg = panic_message(&p);
            error!(run_id = %run_id, seq = seq, message = %msg, "求解过程发生 panic");
            return error_with_run(
                ApiError::new(ErrorCode::SolverFailure, format!("求解器内部错误：{msg}")),
                &run_id,
            );
        }
    };

    let (system, out) = match result {
        Ok(x) => x,
        Err(err) => {
            warn!(
                run_id = %run_id, seq = seq, category = ?err.category, code = ?err.code,
                message = %err.message, "请求被拒绝"
            );
            return error_with_run(err, &run_id);
        }
    };

    let elapsed_ms = started.elapsed().as_millis();
    let mut resp = serde_json::Map::new();
    resp.insert("run_id".into(), serde_json::json!(run_id));
    resp.insert(
        "client_run_id".into(),
        client_run_id
            .map(serde_json::Value::String)
            .unwrap_or(serde_json::Value::Null),
    );
    resp.insert(
        "specification".into(),
        serde_json::json!({"name": system.specification.name, "states": system.specification.num_states(), "edges": system.specification.edges.len()}),
    );
    resp.insert(
        "implementation".into(),
        serde_json::json!({"name": system.implementation.name, "states": system.implementation.num_states(), "edges": system.implementation.edges.len()}),
    );
    resp.insert(
        "observable_alphabet".into(),
        serde_json::json!(system.alphabet),
    );
    resp.insert("elapsed_ms".into(), serde_json::json!(elapsed_ms));
    resp.insert(
        "diagnostics".into(),
        serde_json::to_value(&out.stats).unwrap_or(serde_json::Value::Null),
    );

    match &out.verdict {
        KernelVerdict::Included => {
            resp.insert("verdict".into(), serde_json::json!("included"));
            info!(run_id = %run_id, seq = seq, verdict = "included", elapsed_ms = elapsed_ms,
                nodes = out.stats.search_nodes_visited, layers = out.stats.search_layers,
                "检查完成：成立");
        }
        KernelVerdict::Counterexample {
            trace,
            impl_reach,
            spec_reach,
        } => {
            let trace_names: Vec<&str> = trace
                .iter()
                .map(|&a| system.alphabet[a as usize].as_str())
                .collect();
            // 独立证据验证（不复用求解器内部表）。
            let evidence_report =
                evidence::verify_trace(&system.alphabet, &system.specification, &system.implementation, trace.as_slice());
            resp.insert("verdict".into(), serde_json::json!("counterexample"));
            resp.insert(
                "counterexample".into(),
                serde_json::json!({
                    "trace": trace_names,
                    "length": trace_names.len(),
                    "implementation_reachable": names_of(&system.implementation, impl_reach.as_slice()),
                    "specification_reachable": names_of(&system.specification, spec_reach.as_slice()),
                    "shortest": true,
                    "selection_rule": "长度最短；同长度按 observable_actions 声明序字典序最小",
                }),
            );
            resp.insert(
                "evidence_report".into(),
                serde_json::to_value(&evidence_report).unwrap_or(serde_json::Value::Null),
            );
            info!(
                run_id = %run_id, seq = seq, verdict = "counterexample", elapsed_ms = elapsed_ms,
                trace = ?trace_names, evidence_accepted = evidence_report.accepted,
                nodes = out.stats.search_nodes_visited,
                "检查完成：发现反例"
            );
        }
        KernelVerdict::Unknown { reason } => {
            let code = unknown_code(*reason);
            resp.insert("verdict".into(), serde_json::json!("unknown"));
            resp.insert(
                "unknown".into(),
                serde_json::json!({
                    "reason_code": code,
                    "reason": unknown_message(*reason),
                    "policy": "资源预算耗尽，不给出包含/反例结论；不得解释为成立",
                }),
            );
            warn!(run_id = %run_id, seq = seq, verdict = "unknown", reason = code, elapsed_ms = elapsed_ms,
                "检查中止：状态爆炸/资源耗尽");
        }
    }

    (
        StatusCode::OK,
        [(header::CONTENT_TYPE, "application/json")],
        Json(serde_json::Value::Object(resp)),
    )
        .into_response()
}

fn names_of(lts: &crate::model::Lts, set: &[u32]) -> Vec<String> {
    set.iter().map(|&s| lts.state_name(s).to_owned()).collect()
}

fn unknown_code(reason: UnknownReason) -> &'static str {
    match reason {
        UnknownReason::ClosurePairLimit => "closure_pair_limit",
        UnknownReason::SearchNodeLimit => "search_node_limit",
    }
}

fn unknown_message(reason: UnknownReason) -> String {
    match reason {
        UnknownReason::ClosurePairLimit => {
            "静默闭包/弱像可达二元组数量超过预算，状态空间可能爆炸".to_owned()
        }
        UnknownReason::SearchNodeLimit => {
            "子集对 BFS 访问节点数超过预算，状态空间可能爆炸".to_owned()
        }
    }
}

fn panic_message(p: &Box<dyn std::any::Any + Send>) -> String {
    if let Some(s) = p.downcast_ref::<&'static str>() {
        return (*s).to_owned();
    }
    if let Some(s) = p.downcast_ref::<String>() {
        return s.clone();
    }
    "未知 panic".to_owned()
}
