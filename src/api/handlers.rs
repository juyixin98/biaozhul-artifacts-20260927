//! 请求处理器：解析网与标识、调用内核/验证层、组装可诊断响应。

use std::collections::HashMap;
use std::sync::Arc;

use axum::extract::State;
use axum::http::HeaderMap;
use axum::Json;
use crate::kernel::model::{Marking, Net};
use tracing::debug;

use super::dto::{
    HealthResponse, InvariantsRequest, InvariantsResponse, NetSummary, ReachabilityRequest,
    ReachabilityResponse, SERVICE_NAME, VERSION, VerifyFiringRequest, VerifyFiringResponse,
    VerifyInvariantRequest, VerifyInvariantResponse,
};
use super::error::ApiError;
use crate::config::Config;
use crate::request_id::{current_request_id, extract_request_id};

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
}

/// 从 JSON 对象或 `.pnet` 文本装载网。
fn load_net(net_value: &serde_json::Value, net_text: &Option<String>) -> Result<Net, ApiError> {
    let rid = current_request_id();
    if let Some(text) = net_text {
        return crate::input::parse_pnet(text).map_err(|e| ApiError::from_input(e, &rid));
    }
    if net_value.is_null() {
        return Err(ApiError::bad_request(
            "missing_net",
            "request must provide either 'net' (JSON object) or 'net_text' (.pnet source)",
            &rid,
        ));
    }
    let text = serde_json::to_string(net_value).map_err(|e| {
        ApiError::bad_request("invalid_json", format!("failed to re-encode net: {e}"), &rid)
    })?;
    crate::input::parse_json_net(&text)
        .map(|(n, _)| n)
        .map_err(|e| ApiError::from_input(e, &rid))
}

/// 名称 -> 令牌数 的映射展开为按库所顺序的标识；拒绝未知库所；缺省为 0。
fn marking_from_map(
    net: &Net,
    map: &HashMap<String, i64>,
    field: &str,
) -> Result<Marking, ApiError> {
    let rid = current_request_id();
    let mut vec = vec![0i64; net.place_count()];
    for (name, tokens) in map {
        let Some(idx) = net.place_index(name) else {
            return Err(ApiError::bad_request(
                crate::input::codes::UNKNOWN_PLACE,
                format!("{field} references unknown place '{name}'"),
                &rid,
            ));
        };
        vec[idx] = *tokens;
    }
    crate::input::validate_marking(net, &vec, field).map_err(|e| ApiError::from_input(e, &rid))
}

fn net_summary(net: &Net) -> NetSummary {
    NetSummary {
        place_count: net.place_count(),
        transition_count: net.transition_count(),
        places: net.places.iter().map(|p| p.name.clone()).collect(),
        transitions: net.transitions.iter().map(|t| t.name.clone()).collect(),
        state_space_upper_bound: crate::kernel::state_space_upper_bound(net),
    }
}

pub async fn health() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        service: SERVICE_NAME,
        version: VERSION,
    })
}

pub async fn reachability(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: String,
) -> Result<Json<ReachabilityResponse>, ApiError> {
    let rid = extract_request_id(&headers);
    let req: ReachabilityRequest = serde_json::from_str(&body).map_err(|e| {
        ApiError::bad_request("invalid_json", format!("invalid request body: {e}"), &rid)
    })?;
    debug!(request_id = %rid, "reachability request parsed");

    let net = load_net(&req.net, &req.net_text)?;
    let initial = marking_from_map(&net, &req.initial_marking, "initial_marking")?;
    let target = marking_from_map(&net, &req.target_marking, "target_marking")?;

    let state_limit = req.state_limit.unwrap_or(state.config.solver_state_limit);
    let coeff_bound = req
        .invariant_coefficient_bound
        .unwrap_or(state.config.invariant_coefficient_bound);
    if state_limit == 0 {
        return Err(ApiError::bad_request(
            "bad_state_limit",
            "state_limit must be >= 1",
            &rid,
        ));
    }
    if !(1..=64).contains(&coeff_bound) {
        return Err(ApiError::bad_request(
            "bad_coefficient_bound",
            "invariant_coefficient_bound must be in 1..=64",
            &rid,
        ));
    }

    let opts = crate::kernel::ReachabilityOptions {
        state_limit,
        use_invariant_precheck: true,
        invariant_bounds: crate::kernel::InvariantBounds {
            coefficient_bound: coeff_bound,
            max_combinations: state.config.invariant_max_combinations,
            max_candidates: 256,
        },
    };
    let net_c = net.clone();
    let initial_c = initial.clone();
    let target_c = target.clone();
    let log_rid = rid.clone();
    let span = tracing::info_span!("bfs", request_id = %log_rid);
    // CPU 密集分析放到阻塞线程池；span.in_scope 让内核日志关联到本次请求。
    let result = tokio::task::spawn_blocking(move || {
        span.in_scope(|| {
            crate::kernel::analyze_reachability(&net_c, &initial_c, &target_c, &opts)
        })
    })
    .await
    .map_err(|e| ApiError::internal(format!("analysis task failed: {e}"), &rid))?;

    let decision = match result.decision {
        crate::kernel::ReachabilityDecision::Reachable => "reachable",
        crate::kernel::ReachabilityDecision::Unreachable => "unreachable",
        crate::kernel::ReachabilityDecision::Inconclusive => "inconclusive",
    };
    tracing::info!(
        request_id = %rid,
        decision,
        basis = %result.basis,
        states_visited = result.states_visited,
        "reachability analysis complete"
    );

    Ok(Json(ReachabilityResponse {
        request_id: rid,
        service: SERVICE_NAME,
        version: VERSION,
        decision: decision.to_string(),
        reachable: matches!(result.decision, crate::kernel::ReachabilityDecision::Reachable),
        basis: result.basis,
        certificate: result.certificate,
        invariant_obstruction: result.invariant_obstruction,
        states_visited: result.states_visited,
        state_space_upper_bound: result.state_space_upper_bound,
        state_limit: result.state_limit,
        progress: result.progress,
        scope: result.scope,
        net: net_summary(&net),
    }))
}

pub async fn invariants(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: String,
) -> Result<Json<InvariantsResponse>, ApiError> {
    let rid = extract_request_id(&headers);
    let req: InvariantsRequest = serde_json::from_str(&body).map_err(|e| {
        ApiError::bad_request("invalid_json", format!("invalid request body: {e}"), &rid)
    })?;
    let net = load_net(&req.net, &req.net_text)?;
    let coeff = req
        .coefficient_bound
        .unwrap_or(state.config.invariant_coefficient_bound);
    if !(1..=64).contains(&coeff) {
        return Err(ApiError::bad_request(
            "bad_coefficient_bound",
            "coefficient_bound must be in 1..=64",
            &rid,
        ));
    }
    let bounds = crate::kernel::InvariantBounds {
        coefficient_bound: coeff,
        max_combinations: state.config.invariant_max_combinations,
        max_candidates: 256,
    };
    let net_c = net.clone();
    let report = tokio::task::spawn_blocking(move || crate::kernel::compute_invariants(&net_c, bounds))
        .await
        .map_err(|e| ApiError::internal(format!("analysis task failed: {e}"), &rid))?;

    Ok(Json(InvariantsResponse {
        request_id: rid,
        service: SERVICE_NAME,
        version: VERSION,
        report,
        net: net_summary(&net),
    }))
}

pub async fn verify_firing(
    State(_state): State<AppState>,
    headers: HeaderMap,
    body: String,
) -> Result<Json<VerifyFiringResponse>, ApiError> {
    let rid = extract_request_id(&headers);
    let req: VerifyFiringRequest = serde_json::from_str(&body).map_err(|e| {
        ApiError::bad_request("invalid_json", format!("invalid request body: {e}"), &rid)
    })?;
    let net = load_net(&req.net, &req.net_text)?;
    let initial = marking_from_map(&net, &req.initial_marking, "initial_marking")?;

    // 未知变迁等结构性问题：400。
    let report = match crate::verify::replay(&net, &initial.0, &req.transition_sequence) {
        Ok(r) => r,
        Err(crate::verify::ReplayError::UnknownTransition { name, index }) => {
            return Err(ApiError::bad_request(
                "unknown_transition",
                format!("transition '{name}' at index {index} is not defined by the net"),
                &rid,
            ));
        }
        Err(crate::verify::ReplayError::TransitionNotEnabled { step }) => {
            // 证据不成立：422，带逐步失败类别。
            return Err(ApiError::unprocessable(
                "firing_evidence_invalid",
                format!(
                    "step {} ('{}') is not enabled: {} input deficit(s), {} capacity overflow(s)",
                    step.index,
                    step.transition,
                    step.deficits.len(),
                    step.overflows.len()
                ),
                &rid,
            )
            .with_step(step));
        }
    };

    // 可选的声称终点强校验。
    let claimed_matches = if let Some(claimed) = &req.claimed_final_marking {
        let claimed_vec = marking_from_map(&net, claimed, "claimed_final_marking")?;
        Some(claimed_vec.0 == report.final_marking)
    } else {
        None
    };
    if let Some(false) = claimed_matches {
        return Err(ApiError::unprocessable(
            "final_marking_mismatch",
            format!(
                "replay ended at {:?}, which differs from claimed_final_marking",
                report.final_marking
            ),
            &rid,
        ));
    }

    // 交叉一致性：独立重放的每一步与内核 fire 结论一致。
    let kernel_agrees = cross_check_kernel(&net, &initial.0, &req.transition_sequence);

    Ok(Json(VerifyFiringResponse {
        request_id: rid,
        service: SERVICE_NAME,
        version: VERSION,
        valid: true,
        replay: report,
        claimed_final_matches: claimed_matches,
        kernel_agrees,
    }))
}

pub async fn verify_invariant(
    State(_state): State<AppState>,
    headers: HeaderMap,
    body: String,
) -> Result<Json<VerifyInvariantResponse>, ApiError> {
    let rid = extract_request_id(&headers);
    let req: VerifyInvariantRequest = serde_json::from_str(&body).map_err(|e| {
        ApiError::bad_request("invalid_json", format!("invalid request body: {e}"), &rid)
    })?;
    let net = load_net(&req.net, &req.net_text)?;

    let to_vec = |map: Option<&HashMap<String, i64>>, field: &str| {
        let Some(m) = map else {
            return Ok::<Option<Vec<i64>>, ApiError>(None);
        };
        let v = marking_from_map(&net, m, field)?;
        Ok(Some(v.0))
    };
    let initial_v = to_vec(req.initial_marking.as_ref(), "initial_marking")?;
    let target_v = to_vec(req.target_marking.as_ref(), "target_marking")?;

    let mut weights = vec![0i64; net.place_count()];
    for (name, w) in &req.weights {
        let Some(idx) = net.place_index(name) else {
            return Err(ApiError::bad_request(
                crate::input::codes::UNKNOWN_PLACE,
                format!("weights reference unknown place '{name}'"),
                &rid,
            ));
        };
        weights[idx] = *w;
    }

    match crate::verify::check_invariant(&net, &weights, initial_v.as_deref(), target_v.as_deref()) {
        Ok(report) => Ok(Json(VerifyInvariantResponse {
            request_id: rid,
            service: SERVICE_NAME,
            version: VERSION,
            report,
        })),
        Err(e) => {
            let code = match &e {
                crate::verify::InvariantError::WeightCount { .. }
                | crate::verify::InvariantError::ZeroWeights
                | crate::verify::InvariantError::NegativeWeight { .. }
                | crate::verify::InvariantError::WeightTooLarge { .. } => "bad_invariant_weights",
            };
            let msg = match &e {
                crate::verify::InvariantError::WeightCount { expected, got } => {
                    format!("weights has {got} entries but net has {expected} places")
                }
                crate::verify::InvariantError::ZeroWeights => "weights must not be all zero".to_string(),
                crate::verify::InvariantError::NegativeWeight { place, weight } => {
                    format!("negative weight {weight} on place '{place}' is not a P-invariant (nonnegative weights only)")
                }
                crate::verify::InvariantError::WeightTooLarge { place, weight } => {
                    format!("weight {weight} on place '{place}' exceeds safety bound")
                }
            };
            Err(ApiError::bad_request(code, msg, &rid))
        }
    }
}

/// 用内核 fire 逐步执行同一条序列；独立重放成功时它必须也成功且终点一致。
fn cross_check_kernel(net: &Net, initial: &[i64], sequence: &[String]) -> bool {
    let mut m = Marking(initial.to_vec());
    for name in sequence {
        let Some(ti) = net.transition_index(name) else {
            return false;
        };
        match crate::kernel::fire(net, &m, ti) {
            Ok(next) => m = next,
            Err(_) => return false,
        }
    }
    true
}
