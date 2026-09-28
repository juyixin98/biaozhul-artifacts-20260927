//! Axum wiring: request-id correlation, uniform JSON envelopes, route
//! handlers. The handlers are thin — every substantive step lives in
//! [`crate::service::ErasureCodingService`].


use axum::{
    body::Body,
    extract::State,
    http::{HeaderMap, HeaderName, HeaderValue, Request, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use ec_core::error::EcError;
use ec_core::CodecConfig;
use tower::ServiceBuilder;
use tracing::{error, warn};
use uuid::Uuid;

use crate::dto::{DecodeRequest, EncodeRequest, RepairRequest};
use crate::report::{DecodeReport, RepairReport, VerifyReport};
use crate::service::{b64_decode, ErasureCodingService, InputShard};

/// Shared application state.
#[derive(Clone)]
pub struct AppState {
    pub service: ErasureCodingService,
    pub cfg: CodecConfig,
    pub allow_writes: bool,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/v1/objects", post(encode_object))
        .route("/v1/objects/{object_id}/verify", get(verify_object))
        .route("/v1/objects/{object_id}/decode", post(decode_object))
        .route("/v1/objects/{object_id}/repair", post(repair_object))
        .route("/v1/decode", post(decode_stateless))
        .layer(ServiceBuilder::new().layer(RequestIdLayer))
        .with_state(state)
        .fallback(not_found)
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "status": "ok",
        "service": "ec-service",
        "field": "GF(2^8) mod 0x11B, generator 3",
        "coding": "Reed-Solomon Cauchy, systematic"
    }))
}

async fn not_found() -> ApiError {
    ApiError::new(
        StatusCode::NOT_FOUND,
        "NOT_FOUND",
        "no such endpoint; see POST /v1/objects, GET /v1/objects/:id/verify, POST .../decode, .../repair, POST /v1/decode",
    )
}

// ---------- request-id correlation layer ----------

/// Header carrying the correlation id; clients may supply their own.
pub const REQUEST_ID_HEADER: HeaderName = HeaderName::from_static("x-request-id");

/// Resolve-or-generate the request id and echo it in the response headers.
/// Also emits an access log line so every request is traceable.
#[derive(Clone)]
struct RequestIdLayer;

impl<S> tower::Layer<S> for RequestIdLayer {
    type Service = RequestIdService<S>;
    fn layer(&self, inner: S) -> Self::Service {
        RequestIdService { inner }
    }
}

#[derive(Clone)]
struct RequestIdService<S> {
    inner: S,
}

impl<S> tower::Service<Request<Body>> for RequestIdService<S>
where
    S: tower::Service<Request<Body>, Response = Response> + Send + Clone + 'static,
    S::Future: Send + 'static,
{
    type Response = Response;
    type Error = S::Error;
    type Future = std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<Self::Response, Self::Error>> + Send>,
    >;

    fn poll_ready(
        &mut self,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx)
    }

    fn call(&mut self, mut req: Request<Body>) -> Self::Future {
        let request_id = req
            .headers()
            .get(&REQUEST_ID_HEADER)
            .and_then(|v| v.to_str().ok())
            .filter(|s| !s.is_empty() && s.len() <= 128)
            .map(str::to_string)
            .unwrap_or_else(|| Uuid::new_v4().to_string());
        let method = req.method().clone();
        let path = req.uri().path().to_string();
        req.extensions_mut().insert(RequestId(request_id.clone()));

        let mut inner = self.inner.clone();
        Box::pin(async move {
            tracing::info!(request_id = %request_id, %method, %path, "request start");
            let mut result = inner.call(req).await;
            if let Ok(resp) = &mut result {
                // Echo the correlation id on EVERY response, including errors
                // and handlers that do not use the ok_response helper.
                if let Ok(v) = HeaderValue::from_str(&request_id) {
                    resp.headers_mut().insert(&REQUEST_ID_HEADER, v);
                }
                tracing::info!(
                    request_id = %request_id,
                    %method, %path,
                    status = resp.status().as_u16(),
                    "request end"
                );
            } else {
                tracing::error!(request_id = %request_id, %method, %path, "request failed at service layer");
            }
            result
        })
    }
}

/// Extension holding the correlation id for the duration of a request.
#[derive(Clone)]
struct RequestId(String);

fn request_id(headers: &HeaderMap, ext_id: Option<&RequestId>) -> String {
    if let Some(id) = ext_id {
        return id.0.clone();
    }
    headers
        .get(&REQUEST_ID_HEADER)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string)
        .unwrap_or_else(|| "unknown".into())
}

// ---------- uniform error envelope ----------

#[derive(Debug)]
struct ApiError {
    status: StatusCode,
    code: String,
    message: String,
    request_id: Option<String>,
}

impl ApiError {
    fn new(status: StatusCode, code: &str, message: impl Into<String>) -> Self {
        Self {
            status,
            code: code.to_string(),
            message: message.into(),
            request_id: None,
        }
    }

    fn from_ec(err: EcError) -> Self {
        let status = match &err {
            EcError::InvalidConfig(_)
            | EcError::InvalidShardIndex { .. }
            | EcError::DuplicateShardIndex(_)
            | EcError::SizeMismatch { .. }
            | EcError::ManifestFieldMissing(_) => StatusCode::BAD_REQUEST,
            EcError::DigestMismatch { .. } | EcError::ManifestDigestMismatch => {
                StatusCode::UNPROCESSABLE_ENTITY
            }
            EcError::InsufficientShards { .. } | EcError::NotReconstructable(_) => {
                StatusCode::CONFLICT
            }
            EcError::Store(s) if s.contains("NOT_FOUND") => StatusCode::NOT_FOUND,
            EcError::Store(s) if s.contains("ALREADY_EXISTS") => StatusCode::CONFLICT,
            EcError::Store(_) => StatusCode::INTERNAL_SERVER_ERROR,
            EcError::Internal(_) => StatusCode::INTERNAL_SERVER_ERROR,
            // EcError is #[non_exhaustive]; unknown variants are server errors.
            _ => StatusCode::INTERNAL_SERVER_ERROR,
        };
        let code = err.code().to_string();
        let message = err.to_string();
        if status.is_server_error() {
            error!(%code, %message, "request failed");
        } else {
            warn!(%code, %message, "request rejected");
        }
        Self {
            status,
            code,
            message,
            request_id: None,
        }
    }

    fn with_id(mut self, id: &str) -> Self {
        self.request_id = Some(id.to_string());
        self
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let rid = self.request_id.unwrap_or_else(|| "unknown".into());
        let body = serde_json::json!({
            "ok": false,
            "error": {
                "code": self.code,
                "message": self.message,
            },
            "request_id": rid,
        });
        let mut resp = (self.status, Json(body)).into_response();
        if let Ok(v) = HeaderValue::from_str(&rid) {
            resp.headers_mut().insert(&REQUEST_ID_HEADER, v);
        }
        resp
    }
}

fn ok_response<T: serde::Serialize>(request_id: &str, value: T) -> Response {
    let body = serde_json::json!({
        "ok": true,
        "request_id": request_id,
        "result": serde_json::to_value(value).unwrap_or(serde_json::Value::Null),
    });
    let mut resp = Json(body).into_response();
    if let Ok(v) = HeaderValue::from_str(request_id) {
        resp.headers_mut().insert(&REQUEST_ID_HEADER, v);
    }
    resp
}

/// Map kernel errors into API errors, tagging the request id.
fn map_err(id: &str, e: EcError) -> ApiError {
    ApiError::from_ec(e).with_id(id)
}

// Blocking service work must not run on an async worker; all handlers use
// this helper.
async fn blockify<F, T>(f: F) -> Result<T, EcError>
where
    F: FnOnce() -> Result<T, EcError> + Send + 'static,
    T: Send + 'static,
{
    tokio::task::spawn_blocking(f)
        .await
        .unwrap_or_else(|e| Err(EcError::Internal(format!("worker join failure: {e}"))))
}

// ---------- handlers ----------

async fn encode_object(
    State(state): State<AppState>,
    req: axum::http::Extensions,
    Json(payload): Json<EncodeRequest>,
) -> Result<Response, ApiError> {
    let rid = req
        .get::<RequestId>()
        .map(|r| r.0.clone())
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    if !state.allow_writes {
        return Err(ApiError::new(
            StatusCode::FORBIDDEN,
            "WRITES_DISABLED",
            "this instance runs with allow_writes=false",
        )
        .with_id(&rid));
    }
    let data = b64_decode(&payload.data_b64).map_err(|e| map_err(&rid, e))?;
    let object_id = payload.object_id.unwrap_or_else(|| Uuid::new_v4().to_string());
    let svc = state.service.clone();
    let cfg = state.cfg;
    let op_rid = rid.clone();
    let report = blockify(move || svc.encode_store(&op_rid, object_id, cfg, &data))
        .await
        .map_err(|e| map_err(&rid, e))?;
    Ok(ok_response(&rid, report))
}

async fn verify_object(
    State(state): State<AppState>,
    axum::extract::Path(object_id): axum::extract::Path<String>,
    ext: axum::http::Extensions,
) -> Result<Response, ApiError> {
    let rid = ext
        .get::<RequestId>()
        .map(|r| r.0.clone())
        .unwrap_or_else(|| "unknown".into());
    let svc = state.service.clone();
    let op_rid = rid.clone();
    let report: VerifyReport = blockify(move || svc.verify_store(&op_rid, &object_id))
        .await
        .map_err(|e| map_err(&rid, e))?;
    Ok(ok_response(&rid, report))
}

#[derive(Debug, serde::Deserialize, Default)]
struct DecodeQuery {
    #[serde(default = "default_true")]
    include_data: bool,
}
fn default_true() -> bool {
    true
}

async fn decode_object(
    State(state): State<AppState>,
    axum::extract::Path(object_id): axum::extract::Path<String>,
    axum::extract::Query(q): axum::extract::Query<DecodeQuery>,
    ext: axum::http::Extensions,
) -> Result<Response, ApiError> {
    let rid = ext
        .get::<RequestId>()
        .map(|r| r.0.clone())
        .unwrap_or_else(|| "unknown".into());
    let svc = state.service.clone();
    let op_rid = rid.clone();
    let report: DecodeReport =
        blockify(move || svc.decode_store(&op_rid, &object_id, q.include_data))
            .await
            .map_err(|e| map_err(&rid, e))?;
    // Decode refusal is a logical failure carried inside an HTTP 409.
    if !report.recovered {
        let code = report.failure_code.clone().unwrap_or_else(|| "RECOVERY_FAILED".into());
        return Err(ApiError::new(
            StatusCode::CONFLICT,
            &code,
            report
                .failures
                .first()
                .cloned()
                .unwrap_or_else(|| "object not recoverable from available shards".into()),
        )
        .with_id(&rid));
    }
    Ok(ok_response(&rid, report))
}

async fn repair_object(
    State(state): State<AppState>,
    axum::extract::Path(object_id): axum::extract::Path<String>,
    ext: axum::http::Extensions,
    Json(payload): Json<RepairRequest>,
) -> Result<Response, ApiError> {
    let rid = ext
        .get::<RequestId>()
        .map(|r| r.0.clone())
        .unwrap_or_else(|| "unknown".into());
    if !state.allow_writes {
        return Err(ApiError::new(
            StatusCode::FORBIDDEN,
            "WRITES_DISABLED",
            "this instance runs with allow_writes=false",
        )
        .with_id(&rid));
    }
    let svc = state.service.clone();
    let targets = if payload.targets.is_empty() {
        vec![] // service treats empty as "nothing requested"
    } else {
        payload.targets
    };
    let op_rid = rid.clone();
    let report: RepairReport =
        blockify(move || svc.repair(&op_rid, &object_id, targets, None, true, None))
            .await
            .map_err(|e| map_err(&rid, e))?;
    if !report.repaired {
        let code = report.failure_code.clone().unwrap_or_else(|| "REPAIR_FAILED".into());
        return Err(ApiError::new(
            StatusCode::CONFLICT,
            &code,
            report
                .failures
                .first()
                .cloned()
                .unwrap_or_else(|| "repair impossible from available shards".into()),
        )
        .with_id(&rid));
    }
    Ok(ok_response(&rid, report))
}

async fn decode_stateless(
    State(state): State<AppState>,
    ext: axum::http::Extensions,
    Json(payload): Json<DecodeRequest>,
) -> Result<Response, ApiError> {
    let rid = ext
        .get::<RequestId>()
        .map(|r| r.0.clone())
        .unwrap_or_else(|| Uuid::new_v4().to_string());

    // Stateless path requires manifest + shards; object_id+store is not used.
    let manifest_value = payload.manifest_json.ok_or_else(|| {
        ApiError::new(
            StatusCode::BAD_REQUEST,
            "MANIFEST_FIELD_MISSING",
            "stateless /v1/decode requires `manifest_json`",
        )
        .with_id(&rid)
    })?;
    let manifest_json = serde_json::to_string(&manifest_value).map_err(|e| {
        ApiError::new(
            StatusCode::BAD_REQUEST,
            "INVALID_MANIFEST",
            format!("manifest_json is not serialisable: {e}"),
        )
        .with_id(&rid)
    })?;

    let mut shards: Vec<InputShard> = Vec::with_capacity(payload.shards.len());
    for s in payload.shards {
        let bytes = b64_decode(&s.data_b64).map_err(|e| map_err(&rid, e))?;
        shards.push(InputShard {
            index: s.index,
            bytes,
        });
    }

    let svc = state.service.clone();
    let include_data = payload.include_data;
    let op_rid = rid.clone();
    let report: DecodeReport = blockify(move || {
        svc.decode_stateless(&op_rid, &manifest_json, shards, include_data)
    })
    .await
    .map_err(|e| map_err(&rid, e))?;
    if !report.recovered {
        let code = report.failure_code.clone().unwrap_or_else(|| "RECOVERY_FAILED".into());
        return Err(ApiError::new(
            StatusCode::CONFLICT,
            &code,
            report
                .failures
                .first()
                .cloned()
                .unwrap_or_else(|| "object not recoverable from supplied shards".into()),
        )
        .with_id(&rid));
    }
    Ok(ok_response(&rid, report))
}

// Keep the request_id helper used in some generic paths compiling.
#[allow(dead_code)]
fn rid_from_headers(headers: &HeaderMap) -> String {
    request_id(headers, None)
}
