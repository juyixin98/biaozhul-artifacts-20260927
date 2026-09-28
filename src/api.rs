//! 后端接口：Axum 路由、请求/响应 DTO、诊断日志。
//!
//! 路由：
//! - `GET  /healthz`：存活检查。
//! - `POST /solve`：解析 → 规范化 → 求解 → 独立复核 → 返回带证据的结论。
//!
//! 每个响应都带 `request_id`（请求未提供时生成），诊断日志以同一 id 关联，
//! 但日志只记录规模/结论/预算等元数据，不打印原始公式内容（避免敏感数据泄漏）。

use std::sync::Arc;

use axum::body::Bytes;
use axum::extract::{FromRequest, Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::json;
use tracing::{info, warn};
use uuid::Uuid;

use crate::cnf::{normalize_formula, NormNote};
use crate::config::Config;
use crate::evidence::ResolutionProof;
use crate::input::JsonCnfRequest;
use crate::solver::{SolveLimits, Solver, Status, StopReason};
use crate::verify::{check_model, check_proof};

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
}

/// 带统一错误体的 JSON 提取器：畸形/空请求也返回 400 + category + request_id，
/// 而不是框架默认的 422。
struct JsonReq<T>(T);

#[axum::async_trait]
impl<S, T> FromRequest<S> for JsonReq<T>
where
    S: Send + Sync,
    T: DeserializeOwned,
{
    type Rejection = ApiError;

    async fn from_request(req: Request, state: &S) -> Result<Self, Self::Rejection> {
        let request_id = Uuid::new_v4().to_string();
        let bytes = match Bytes::from_request(req, state).await {
            Ok(b) => b,
            Err(e) => {
                return Err(ApiError {
                    status: StatusCode::BAD_REQUEST,
                    category: "malformed_request",
                    message: e.body_text(),
                    request_id,
                });
            }
        };
        serde_json::from_slice::<T>(&bytes)
            .map(JsonReq)
            .map_err(|e| ApiError {
                status: StatusCode::BAD_REQUEST,
                category: "malformed_json",
                message: e.to_string(),
                request_id,
            })
    }
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/solve", post(solve))
        .route("/verify", post(verify_evidence))
        .with_state(state)
}

async fn healthz() -> Json<serde_json::Value> {
    Json(json!({ "status": "ok" }))
}

/// 请求里的可选预算覆盖。字段省略时回退到服务端配置；
/// 想显式取消某类预算，传 `-1`。
#[derive(Debug, Clone, Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct RequestLimits {
    max_decisions: Option<i64>,
    time_limit_ms: Option<i64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SolveRequest {
    #[serde(flatten)]
    formula: JsonCnfRequest,
    #[serde(default)]
    limits: RequestLimits,
    /// 调用方自带的关联 id；省略则服务端生成。
    request_id: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "snake_case")]
enum Verdict {
    Sat,
    Unsat,
    Unknown,
}

#[derive(Debug, Serialize)]
struct SolveResponse {
    request_id: String,
    verdict: Verdict,
    /// 接受/拒绝/无法判定的原因摘要。
    conclusion: String,
    num_vars: usize,
    /// DIMACS 风格有符号文字：模型中为真的变量给 `v`，为假给 `-v`。
    model: Option<Vec<i32>>,
    proof: Option<ResolutionProof>,
    normalization: Vec<NormNote>,
    /// 检查器对证据的最终意见；UNKNOWN 时为 null。
    evidence_check: Option<EvidenceCheck>,
    diagnostics: serde_json::Value,
}

#[derive(Debug, Serialize)]
#[serde(tag = "result", rename_all = "snake_case")]
enum EvidenceCheck {
    Accepted {
        what: String,
    },
    Rejected {
        what: String,
        error: serde_json::Value,
    },
}

#[derive(Debug, Serialize)]
struct ErrorBody {
    request_id: String,
    error: String,
    category: &'static str,
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = ErrorBody {
            request_id: self.request_id,
            error: self.message,
            category: self.category,
        };
        (self.status, Json(body)).into_response()
    }
}

struct ApiError {
    status: StatusCode,
    category: &'static str,
    message: String,
    request_id: String,
}

impl ApiError {
    fn bad_input(request_id: &str, message: impl std::fmt::Display) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            category: "invalid_input",
            message: message.to_string(),
            request_id: request_id.to_string(),
        }
    }
}

async fn solve(
    State(state): State<AppState>,
    JsonReq(req): JsonReq<SolveRequest>,
) -> Result<Json<SolveResponse>, ApiError> {
    let request_id = req
        .request_id
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| Uuid::new_v4().to_string());

    let parsed = req
        .formula
        .into_parsed()
        .map_err(|e| ApiError::bad_input(&request_id, e))?;
    let (formula, notes) = normalize_formula(parsed.declared_vars, &parsed.clauses)
        .map_err(|e| ApiError::bad_input(&request_id, e))?;

    let limits = SolveLimits {
        max_decisions: match req.limits.max_decisions {
            Some(-1) => None,
            Some(n) if n >= 0 => Some(n as u64),
            Some(bad) => {
                return Err(ApiError {
                    status: StatusCode::BAD_REQUEST,
                    category: "invalid_input",
                    message: format!("limits.max_decisions must be >= -1, got {bad}"),
                    request_id,
                });
            }
            None => state.config.max_decisions,
        },
        time_limit: match req.limits.time_limit_ms {
            Some(-1) => None,
            Some(ms) if ms >= 0 => Some(std::time::Duration::from_millis(ms as u64)),
            Some(bad) => {
                return Err(ApiError {
                    status: StatusCode::BAD_REQUEST,
                    category: "invalid_input",
                    message: format!("limits.time_limit_ms must be >= -1, got {bad}"),
                    request_id,
                });
            }
            None => state.config.time_limit,
        },
    };

    let num_input_clauses = formula.clauses.len();
    let outcome = Solver::new(&formula).solve(&limits);

    // 独立复核：模型与推导必须通过检查器，否则对外只能按“内部错误”拒绝，绝不冒充结论。
    let mut evidence_check = None;
    let mut verdict = match outcome.status {
        Status::Sat => Verdict::Sat,
        Status::Unsat => Verdict::Unsat,
        Status::Unknown => Verdict::Unknown,
    };

    let conclusion;
    let mut model_out = None;
    let mut proof_out = None;

    match outcome.status {
        Status::Sat => {
            let model = outcome.model.clone().expect("SAT carries a model");
            match check_model(&formula, &model) {
                Ok(()) => {
                    evidence_check = Some(EvidenceCheck::Accepted {
                        what: "model".into(),
                    });
                    conclusion = "SAT: independent checker accepted the complete model".into();
                    model_out = Some(dimacs_model(&model));
                }
                Err(e) => {
                    warn!(request_id = %request_id, error = ?e, "self-generated model rejected by checker");
                    evidence_check = Some(EvidenceCheck::Rejected {
                        what: "model".into(),
                        error: serde_json::to_value(&e)
                            .unwrap_or(json!({"error": "unserializable"})),
                    });
                    verdict = Verdict::Unknown;
                    conclusion =
                        "solver claimed SAT but its model failed independent verification".into();
                }
            }
        }
        Status::Unsat => {
            let proof = outcome.proof.clone().expect("UNSAT carries a proof");
            match check_proof(&formula, &proof) {
                Ok(()) => {
                    evidence_check = Some(EvidenceCheck::Accepted {
                        what: "resolution_proof".into(),
                    });
                    conclusion =
                        "UNSAT: independent checker replayed the resolution proof to empty clause"
                            .into();
                    proof_out = Some(proof);
                }
                Err(e) => {
                    warn!(request_id = %request_id, error = ?e, "self-generated proof rejected by checker");
                    evidence_check = Some(EvidenceCheck::Rejected {
                        what: "resolution_proof".into(),
                        error: serde_json::to_value(&e)
                            .unwrap_or(json!({"error": "unserializable"})),
                    });
                    verdict = Verdict::Unknown;
                    conclusion =
                        "solver claimed UNSAT but its proof failed independent verification".into();
                }
            }
        }
        Status::Unknown => {
            conclusion = match outcome.diagnostics.stop_reason {
                StopReason::DecisionBudget => {
                    "UNKNOWN: decision budget exhausted; result must not be interpreted as UNSAT"
                        .into()
                }
                StopReason::TimeBudget => {
                    "UNKNOWN: time budget exhausted; result must not be interpreted as UNSAT".into()
                }
                StopReason::Completed => "UNKNOWN: solver stopped without a verdict".into(),
            };
        }
    }

    info!(
        request_id = %request_id,
        num_vars = formula.num_vars,
        num_input_clauses = num_input_clauses,
        decisions = outcome.diagnostics.decisions,
        conflicts = outcome.diagnostics.conflicts,
        stop_reason = ?outcome.diagnostics.stop_reason,
        verdict = ?verdict,
        "solve complete"
    );

    Ok(Json(SolveResponse {
        request_id,
        verdict,
        conclusion,
        num_vars: formula.num_vars,
        model: model_out,
        proof: proof_out,
        normalization: notes,
        evidence_check,
        diagnostics: serde_json::to_value(&outcome.diagnostics).unwrap_or(json!({})),
    }))
}

fn dimacs_model(model: &[bool]) -> Vec<i32> {
    (1..model.len())
        .map(|v| if model[v] { v as i32 } else { -(v as i32) })
        .collect()
}

/// 供测试/外部校验使用：把 DIMACS 风格模型转回内部真值向量。
pub fn model_from_dimacs(num_vars: usize, lits: &[i32]) -> Result<Vec<bool>, String> {
    let mut m = vec![false; num_vars + 1];
    for &l in lits {
        if l == 0 {
            return Err("literal 0 is not part of a model".into());
        }
        let v = l.unsigned_abs() as usize;
        if v == 0 || v > num_vars {
            return Err(format!("model literal {l} out of range 1..={num_vars}"));
        }
        m[v] = l > 0;
    }
    Ok(m)
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct VerifyRequest {
    #[serde(flatten)]
    formula: JsonCnfRequest,
    /// DIMACS 风格有符号文字数组；与 `proof` 二选一。
    model: Option<Vec<i32>>,
    proof: Option<ResolutionProof>,
    request_id: Option<String>,
}

#[derive(Debug, Serialize)]
struct VerifyResponse {
    request_id: String,
    accepted: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<serde_json::Value>,
}

/// `POST /verify`：对外部任意证据（可能已被篡改）做独立复核。
async fn verify_evidence(
    State(state): State<AppState>,
    JsonReq(req): JsonReq<VerifyRequest>,
) -> Result<Json<VerifyResponse>, ApiError> {
    let _ = &state;
    let request_id = req
        .request_id
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| Uuid::new_v4().to_string());

    let parsed = req
        .formula
        .into_parsed()
        .map_err(|e| ApiError::bad_input(&request_id, e))?;
    let (formula, _notes) = normalize_formula(parsed.declared_vars, &parsed.clauses)
        .map_err(|e| ApiError::bad_input(&request_id, e))?;

    let (accepted, error) = match (req.model, req.proof) {
        (Some(lits), None) => match model_from_dimacs(formula.num_vars, &lits) {
            Ok(m) => match check_model(&formula, &m) {
                Ok(()) => (true, None),
                Err(e) => (false, Some(serde_json::to_value(e).unwrap())),
            },
            Err(msg) => (
                false,
                Some(json!({ "kind": "bad_model_literal", "message": msg })),
            ),
        },
        (None, Some(proof)) => match check_proof(&formula, &proof) {
            Ok(()) => (true, None),
            Err(e) => (false, Some(serde_json::to_value(e).unwrap())),
        },
        (Some(_), Some(_)) => {
            return Err(ApiError {
                status: StatusCode::BAD_REQUEST,
                category: "invalid_input",
                message: "provide exactly one of `model` or `proof`".into(),
                request_id,
            });
        }
        (None, None) => {
            return Err(ApiError {
                status: StatusCode::BAD_REQUEST,
                category: "invalid_input",
                message: "provide one of `model` or `proof`".into(),
                request_id,
            });
        }
    };

    info!(
        request_id = %request_id,
        num_vars = formula.num_vars,
        accepted,
        "external evidence verification complete"
    );
    Ok(Json(VerifyResponse {
        request_id,
        accepted,
        error,
    }))
}
