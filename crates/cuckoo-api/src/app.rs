//! 应用状态、路由与处理器。

use std::sync::{Arc, Mutex};

use axum::extract::{Request, State};
use axum::http::HeaderMap;
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tower::ServiceBuilder;
use tracing::{debug, info, instrument};

use cuckoo_persist::ServiceState;

use crate::encoding::{decode_key, preview, KeyEncoding};
use crate::error::ApiError;

#[derive(Clone)]
pub struct AppState {
    pub svc: Arc<Mutex<ServiceState>>,
    pub started_at: std::time::SystemTime,
    pub run_id: String,
}

pub fn router(state: AppState) -> Router {
    let req_id = ServiceBuilder::new().layer(axum::middleware::from_fn_with_state(
        state.clone(),
        request_id_layer,
    ));
    Router::new()
        .route("/health", get(health))
        .route("/v1/filter/insert", post(insert))
        .route("/v1/filter/lookup", post(lookup))
        .route("/v1/filter/delete", post(delete))
        .route("/v1/filter/stats", get(stats))
        .layer(req_id)
        .with_state(state)
}

async fn request_id_layer(
    State(st): State<AppState>,
    headers: HeaderMap,
    req: Request,
    next: Next,
) -> Response {
    // 请求身份：优先沿用调用方 X-Request-Id，否则本地生成；写入响应头与日志 span。
    let rid = headers
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
        .unwrap_or_else(|| {
            format!(
                "{}-{:x}",
                st.run_id,
                std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0)
            )
        });
    let span = tracing::info_span!("http", request_id = %rid);
    let mut resp = {
        let _g = span.enter();
        debug!(method = %req.method(), uri = %req.uri(), "收到请求");
        next.run(req).await
    };
    resp.headers_mut().insert(
        "x-request-id",
        rid.parse().unwrap_or_else(|_| {
        axum::http::HeaderValue::from_str("unrepresentable").unwrap()
        }),
    );
    resp
}

#[derive(Debug, Deserialize)]
pub struct KeyBody {
    pub key: String,
    #[serde(default)]
    pub key_encoding: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct DeleteBody {
    pub key: String,
    pub credential: String,
    #[serde(default)]
    pub key_encoding: Option<String>,
}

#[derive(Debug, Serialize)]
struct VersionInfo {
    core_version: &'static str,
    format_version: u32,
    run_id: String,
}

fn decode(body: &KeyBody) -> Result<(Vec<u8>, KeyEncoding), ApiError> {
    let enc = KeyEncoding::parse(body.key_encoding.as_deref())
        .map_err(ApiError::bad_request)?;
    decode_key(&body.key, enc).map_err(ApiError::bad_request).map(|k| (k, enc))
}

async fn health(State(st): State<AppState>) -> impl IntoResponse {
    let svc = st.svc.lock().expect("状态锁不应中毒");
    Json(json!({
        "ok": true,
        "status": "serving",
        "version": VersionInfo {
            core_version: cuckoo_core::CORE_VERSION,
            format_version: cuckoo_core::FORMAT_VERSION,
            run_id: st.run_id.clone(),
        },
        "generation": svc.generation(),
    }))
}

#[instrument(skip_all)]
async fn insert(
    State(st): State<AppState>,
    Json(body): Json<KeyBody>,
) -> Result<Response, ApiError> {
    let (key, enc) = decode(&body)?;
    info!(
        key = %preview(&body.key, enc),
        key_encoding = enc.as_str(),
        key_bytes = key.len(),
        "插入请求"
    );
    let mut svc = st.svc.lock().expect("状态锁不应中毒");
    let gen_before = svc.generation();
    match svc.insert(&key) {
        Ok(ins) => {
            info!(
                generation_before = gen_before,
                generation_after = svc.generation(),
                kicks = ins.kicks,
                placed = %format!("b{}/s{}", ins.placed_bucket, ins.placed_slot),
                load_factor = format!("{:.6}", ins.load_factor),
                "插入成功"
            );
            Ok(Json(json!({
                "ok": true,
                "credential": ins.token,
                "detail": {
                    "jti_b64url": ins.token.split('.').next().unwrap_or(""),
                    "kicks": ins.kicks,
                    "placed_bucket": ins.placed_bucket,
                    "placed_slot": ins.placed_slot,
                    "load_factor": ins.load_factor,
                    "generation": svc.generation(),
                }
            }))
            .into_response())
        }
        Err(e) => {
            let api: ApiError = e.into();
            info!(error_code = api.code, "插入失败: {}", api.message);
            Err(api)
        }
    }
}

#[instrument(skip_all)]
async fn lookup(
    State(st): State<AppState>,
    Json(body): Json<KeyBody>,
) -> Result<Response, ApiError> {
    let (key, enc) = decode(&body)?;
    let svc = st.svc.lock().expect("状态锁不应中毒");
    let info = svc.lookup(&key);
    debug!(
        key = %preview(&body.key, enc),
        key_encoding = enc.as_str(),
        member = info.member,
        observed_copies = info.observed_copies,
        generation = svc.generation(),
        "近似查询"
    );
    Ok(Json(json!({
        "ok": true,
        "member": info.member,
        "observed_copies": info.observed_copies,
        "note": "近似结果：可能假阳性；正常数据不会假阴性",
        "generation": svc.generation(),
    }))
    .into_response())
}

#[instrument(skip_all)]
async fn delete(
    State(st): State<AppState>,
    Json(body): Json<DeleteBody>,
) -> Result<Response, ApiError> {
    let enc = KeyEncoding::parse(body.key_encoding.as_deref()).map_err(ApiError::bad_request)?;
    let key = decode_key(&body.key, enc).map_err(ApiError::bad_request)?;
    info!(
        key = %preview(&body.key, enc),
        key_encoding = enc.as_str(),
        key_bytes = key.len(),
        credential_jti = %body.credential.split('.').next().unwrap_or(""),
        "删除请求"
    );

    let mut svc = st.svc.lock().expect("状态锁不应中毒");
    match svc.delete(&key, &body.credential) {
        Ok(rem) => {
            info!(
                removed = %format!("b{}/s{}", rem.bucket, rem.slot),
                load_factor = format!("{:.6}", rem.load_factor),
                generation = svc.generation(),
                "删除成功（凭凭证移除本键副本）"
            );
            Ok(Json(json!({
                "ok": true,
                "removed": true,
                "detail": {
                    "bucket": rem.bucket,
                    "slot": rem.slot,
                    "load_factor": rem.load_factor,
                    "generation": svc.generation(),
                }
            }))
            .into_response())
        }
        Err(e) => {
            let api: ApiError = e.into();
            info!(error_code = api.code, "删除被拒绝: {}", api.message);
            Err(api)
        }
    }
}

async fn stats(State(st): State<AppState>) -> impl IntoResponse {
    let svc = st.svc.lock().expect("状态锁不应中毒");
    let s = svc.stats();
    let v: Value = serde_json::to_value(&s).unwrap_or(json!({}));
    Json(json!({
        "ok": true,
        "stats": v,
        "generation": svc.generation(),
    }))
}
