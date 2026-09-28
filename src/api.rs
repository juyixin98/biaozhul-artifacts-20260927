//! Axum HTTP layer: stateless encode/decode endpoints plus object-store
//! endpoints backed by [`crate::store::ObjectStore`].
//!
//! Every request gets a `run_id` (see [`crate::runid`]) attached to tracing
//! events and returned as the `x-run-id` response header, so logs correlate
//! to inputs. Codec/container failures are never reported as success: the
//! stable category string from [`crate::error::ErrorKind`] is the JSON
//! `error` field with an appropriate 4xx status.

use crate::config::Config;
use crate::container::{decode_container, encode_container, inspect, ContainerInfo, DecodedBlock};
use crate::error::{Error, ErrorKind};
use crate::runid::RunId;
use crate::store::{ObjectInfo, ObjectStore};
use axum::{
    body::Bytes,
    extract::{Extension, Path, State},
    http::{header, HeaderMap, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post, put},
    Router,
};
use std::sync::Arc;
use tracing::{debug, info, warn};

/// JSON response newtype so handlers can return `serde_json::Value`-shaped
/// literals while satisfying axum's `IntoResponse` requirement.
#[derive(Clone)]
pub struct JsonResp(pub serde_json::Value);

impl IntoResponse for JsonResp {
    fn into_response(self) -> Response {
        (
            [(header::CONTENT_TYPE, "application/json")],
            axum::Json(self.0),
        )
            .into_response()
    }
}

/// Shorthand for `Ok(JsonResp(serde_json::json! { ... }))`.
macro_rules! jjson {
    ($($t:tt)*) => {
        Ok($crate::api::JsonResp(serde_json::json!($($t)*)))
    };
}

/// Shared application state.
#[derive(Clone)]
pub struct AppState {
    /// Resolved configuration.
    pub config: Arc<Config>,
    /// Object storage backend.
    pub store: Arc<dyn ObjectStore>,
}

impl AppState {
    /// Build state from config and a pre-constructed store.
    pub fn new(config: Config, store: Arc<dyn ObjectStore>) -> Self {
        AppState {
            config: Arc::new(config),
            store,
        }
    }
}

/// Build the application router.
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/api/v1/encode", post(encode_body))
        .route("/api/v1/decode", post(decode_body))
        .route("/api/v1/verify", post(verify_body))
        .route("/api/v1/objects", put(create_object).post(list_objects))
        .route(
            "/api/v1/objects/:id",
            get(get_object).put(put_object).delete(delete_object),
        )
        .route("/api/v1/objects/:id/meta", get(object_meta))
        .route("/api/v1/objects/:id/verify", post(verify_object))
        .layer(axum::middleware::from_fn(run_id_layer))
        .with_state(state)
}

/// Middleware that assigns (or honours an inbound) run id, stores it in
/// request extensions and echoes it on the response.
async fn run_id_layer(
    mut req: axum::extract::Request,
    next: axum::middleware::Next,
) -> axum::response::Response {
    let run_id = req
        .headers()
        .get("x-run-id")
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty() && s.len() <= 128 && s.is_ascii())
        .map(|s| RunId(s.to_string()))
        .unwrap_or_else(|| RunId(crate::runid::generate()));

    let method = req.method().clone();
    let path = req.uri().path().to_string();
    info!(run_id = run_id.as_str(), method = %method, path = %path, "request start");

    req.extensions_mut().insert(run_id.clone());
    let mut response = next.run(req).await;
    response
        .headers_mut()
        .insert("x-run-id", run_id.header_value());
    info!(
        run_id = run_id.as_str(),
        status = %response.status().as_u16(),
        "request end"
    );
    response
}

async fn healthz() -> &'static str {
    "ok\n"
}

/// Split raw input into blocks at the configured block threshold.
fn split_blocks(input: &[u8], block_len: usize) -> Vec<Vec<u8>> {
    if input.is_empty() {
        // One empty block round-trips the empty input explicitly.
        return vec![Vec::new()];
    }
    input.chunks(block_len).map(|c| c.to_vec()).collect()
}

fn info_to_json(info: &ContainerInfo, object_id: Option<&str>) -> serde_json::Value {
    serde_json::json!({
        "format": "HCMP",
        "version": info.version,
        "block_count": info.blocks.len(),
        "blocks": info.blocks.iter().map(|b| serde_json::json!({
            "block_id": b.block_id,
            "original_len": b.original_len,
            "payload_len": b.payload_len,
            "original_crc32": format!("{:08x}", b.original_crc),
        })).collect::<Vec<_>>(),
        "object_id": object_id,
    })
}

async fn encode_body(
    State(state): State<AppState>,
    Extension(run): Extension<RunId>,
    body: Bytes,
) -> ApiResult<Response> {
    check_body_limit(&state, body.len())?;
    let chunks = split_blocks(&body, state.config.max_block_len as usize);
    info!(
        run_id = run.as_str(),
        input_len = body.len(),
        blocks = chunks.len(),
        max_block_len = state.config.max_block_len,
        "encoding body"
    );
    let encoded = encode_container(&chunks, state.config.max_block_len)
        .map_err(|e| map_codec_error(e, run.as_str(), "encode"))?;
    debug!(
        run_id = run.as_str(),
        container_len = encoded.len(),
        "encoding complete"
    );
    Ok(binary_response(encoded, "application/vnd.hcomp"))
}

async fn decode_body(
    State(state): State<AppState>,
    Extension(run): Extension<RunId>,
    body: Bytes,
) -> ApiResult<Response> {
    let blocks = decode_bytes(&state, &body, run.as_str(), "decode")?;
    let total_len: usize = blocks.iter().map(|b| b.data.len()).sum();
    let mut total = Vec::with_capacity(total_len);
    for b in blocks {
        total.extend_from_slice(&b.data);
    }
    Ok(binary_response(total, "application/octet-stream"))
}

async fn verify_body(
    State(state): State<AppState>,
    Extension(run): Extension<RunId>,
    body: Bytes,
) -> ApiResult<JsonResp> {
    let info = inspect(&body).map_err(|e| map_codec_error(e, run.as_str(), "verify-structure"))?;
    let blocks = decode_container(&body, state.config.max_block_len)
        .map_err(|e| map_codec_error(e, run.as_str(), "verify-decode"))?;
    let decoded_total: u64 = blocks.iter().map(|b| b.meta.original_len).sum();
    info!(
        run_id = run.as_str(),
        blocks = blocks.len(),
        decoded_total,
        "verify ok"
    );
    jjson!({
        "ok": true,
        "run_id": run.as_str(),
        "info": info_to_json(&info, None),
        "decoded_total_len": decoded_total,
    })
}

async fn create_object(
    State(state): State<AppState>,
    Extension(run): Extension<RunId>,
    headers: HeaderMap,
    body: Bytes,
) -> ApiResult<(StatusCode, JsonResp)> {
    let id = headers
        .get("x-object-id")
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            api_error(
                ErrorKind::InvalidId,
                "PUT /api/v1/objects requires a non-empty x-object-id header",
            )
        })?
        .to_string();
    put_with_id(&state, id, body, false, run.as_str()).await
}

async fn put_object(
    State(state): State<AppState>,
    Extension(run): Extension<RunId>,
    Path(id): Path<String>,
    body: Bytes,
) -> ApiResult<(StatusCode, JsonResp)> {
    put_with_id(&state, id, body, true, run.as_str()).await
}

async fn put_with_id(
    state: &AppState,
    id: String,
    body: Bytes,
    overwrite: bool,
    run_id: &str,
) -> ApiResult<(StatusCode, JsonResp)> {
    check_body_limit(state, body.len())?;
    let chunks = split_blocks(&body, state.config.max_block_len as usize);
    let encoded = encode_container(&chunks, state.config.max_block_len)
        .map_err(|e| map_codec_error(e, run_id, "encode"))?;
    let info = inspect(&encoded).expect("freshly encoded containers must inspect");
    state
        .store
        .put(&id, &encoded, overwrite)
        .await
        .map_err(|e| map_store_error(e, run_id))?;
    info!(
        run_id,
        object_id = %id,
        blocks = info.blocks.len(),
        container_len = encoded.len(),
        overwrite,
        "object stored"
    );
    Ok((
        if overwrite {
            StatusCode::OK
        } else {
            StatusCode::CREATED
        },
        JsonResp(serde_json::json!({
            "ok": true,
            "run_id": run_id,
            "object_id": id,
            "info": info_to_json(&info, Some(&id)),
        })),
    ))
}

async fn get_object(
    State(state): State<AppState>,
    Extension(run): Extension<RunId>,
    Path(id): Path<String>,
) -> ApiResult<Response> {
    let bytes = state
        .store
        .get(&id)
        .await
        .map_err(|e| map_store_error(e, run.as_str()))?;
    let blocks = decode_bytes(&state, &bytes, run.as_str(), "object-decode")?;
    let total_len: usize = blocks.iter().map(|b| b.data.len()).sum();
    let mut total = Vec::with_capacity(total_len);
    for b in blocks {
        total.extend_from_slice(&b.data);
    }
    Ok(binary_response(total, "application/octet-stream"))
}

async fn object_meta(
    State(state): State<AppState>,
    Extension(run): Extension<RunId>,
    Path(id): Path<String>,
) -> ApiResult<JsonResp> {
    let obj = state
        .store
        .stat(&id)
        .await
        .map_err(|e| map_store_error(e, run.as_str()))?;
    let bytes = state
        .store
        .get(&id)
        .await
        .map_err(|e| map_store_error(e, run.as_str()))?;
    let info = inspect(&bytes).map_err(|e| map_codec_error(e, run.as_str(), "meta-inspect"))?;
    jjson!({
        "ok": true,
        "run_id": run.as_str(),
        "object": object_info_json(&obj),
        "info": info_to_json(&info, Some(&id)),
    })
}

async fn verify_object(
    State(state): State<AppState>,
    Extension(run): Extension<RunId>,
    Path(id): Path<String>,
) -> ApiResult<JsonResp> {
    let bytes = state
        .store
        .get(&id)
        .await
        .map_err(|e| map_store_error(e, run.as_str()))?;
    let info = inspect(&bytes).map_err(|e| map_codec_error(e, run.as_str(), "verify-structure"))?;
    let blocks = decode_container(&bytes, state.config.max_block_len)
        .map_err(|e| map_codec_error(e, run.as_str(), "verify-decode"))?;
    let decoded_total: u64 = blocks.iter().map(|b| b.meta.original_len).sum();
    info!(
        run_id = run.as_str(),
        object_id = %id,
        blocks = blocks.len(),
        "object verified"
    );
    jjson!({
        "ok": true,
        "run_id": run.as_str(),
        "object_id": id,
        "info": info_to_json(&info, Some(&id)),
        "decoded_total_len": decoded_total,
    })
}

async fn list_objects(
    State(state): State<AppState>,
    Extension(run): Extension<RunId>,
) -> ApiResult<JsonResp> {
    let objects = state
        .store
        .list()
        .await
        .map_err(|e| map_store_error(e, run.as_str()))?;
    jjson!({
        "ok": true,
        "run_id": run.as_str(),
        "count": objects.len(),
        "objects": objects.iter().map(object_info_json).collect::<Vec<_>>(),
    })
}

async fn delete_object(
    State(state): State<AppState>,
    Extension(run): Extension<RunId>,
    Path(id): Path<String>,
) -> ApiResult<JsonResp> {
    state
        .store
        .delete(&id)
        .await
        .map_err(|e| map_store_error(e, run.as_str()))?;
    info!(run_id = run.as_str(), object_id = %id, "object deleted");
    jjson!({ "ok": true, "run_id": run.as_str(), "object_id": id })
}

fn check_body_limit(state: &AppState, len: usize) -> ApiResult<()> {
    if len > state.config.max_body_len {
        return Err(api_error(
            ErrorKind::BlockTooLarge,
            format!(
                "request body {len} bytes exceeds max_body_len {}",
                state.config.max_body_len
            ),
        ));
    }
    Ok(())
}

fn decode_bytes(
    state: &AppState,
    bytes: &[u8],
    run_id: &str,
    stage: &str,
) -> ApiResult<Vec<DecodedBlock>> {
    decode_container(bytes, state.config.max_block_len)
        .map_err(|e| map_codec_error(e, run_id, stage))
}

fn object_info_json(o: &ObjectInfo) -> serde_json::Value {
    serde_json::json!({
        "id": o.id,
        "size": o.size,
        "modified_unix": o.modified_unix,
    })
}

fn binary_response(bytes: Vec<u8>, content_type: &'static str) -> Response {
    let mut resp = Response::new(bytes.into());
    resp.headers_mut()
        .insert(header::CONTENT_TYPE, HeaderValue::from_static(content_type));
    resp
}

/// Axum error carrying a status and a JSON body.
struct ApiError {
    status: StatusCode,
    body: serde_json::Value,
}

/// Handler result alias: success `T`, failure our JSON [`ApiError`].
type ApiResult<T> = Result<T, ApiError>;

impl std::fmt::Debug for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "ApiError({:?}, {})", self.status, self.body)
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let mut resp = Response::new(axum::body::Body::from(self.body.to_string()));
        *resp.status_mut() = self.status;
        resp.headers_mut().insert(
            header::CONTENT_TYPE,
            HeaderValue::from_static("application/json"),
        );
        resp
    }
}

fn api_error(kind: ErrorKind, detail: impl Into<String>) -> ApiError {
    ApiError {
        status: StatusCode::from_u16(kind.http_status())
            .unwrap_or(StatusCode::UNPROCESSABLE_ENTITY),
        body: serde_json::json!({
            "ok": false,
            "error": kind.as_str(),
            "detail": detail.into(),
        }),
    }
}

fn map_codec_error(e: Error, run_id: &str, stage: &str) -> ApiError {
    warn!(
        run_id,
        stage,
        error = e.kind().as_str(),
        detail = %e.context(),
        "request failed"
    );
    api_error(e.kind(), e.context().to_string())
}

fn map_store_error(e: Error, run_id: &str) -> ApiError {
    warn!(
        run_id,
        error = e.kind().as_str(),
        detail = %e.context(),
        "store operation failed"
    );
    api_error(e.kind(), e.context().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::FileSystemStore;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    // A single shared backing directory for the whole test module, so every
    // request in the lifecycle test hits the same store.
    static SHARED_DIR: tokio::sync::OnceCell<std::sync::Mutex<Option<tempfile::TempDir>>> =
        tokio::sync::OnceCell::const_new();

    async fn test_state() -> AppState {
        let cell = SHARED_DIR
            .get_or_init(|| async { std::sync::Mutex::new(Some(tempfile::tempdir().unwrap())) })
            .await;
        // SAFETY/aliasing: each call builds a fresh store rooted at the same
        // directory; the TempDir itself is retained by the OnceCell for the
        // process lifetime, so the path stays valid.
        let root = cell.lock().unwrap().as_ref().unwrap().path().to_path_buf();
        let store = FileSystemStore::new(root).await.unwrap();
        // Small block limit forces multi-block splitting in tests.
        let cfg = Config {
            max_block_len: 64,
            max_body_len: 4096,
            ..Config::default()
        };
        AppState::new(cfg, Arc::new(store))
    }

    async fn body(resp: Response) -> Vec<u8> {
        axum::body::to_bytes(resp.into_body(), 1 << 20)
            .await
            .unwrap()
            .to_vec()
    }

    #[tokio::test]
    async fn health() {
        let app = router(test_state().await);
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/healthz")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn encode_decode_roundtrip_and_verify() {
        let app = router(test_state().await);
        let payload = b"canonical huffman round trip!! aaaaa bbbbb".to_vec();

        let enc = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/encode")
                    .header("content-type", "application/octet-stream")
                    .body(Body::from(payload.clone()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(enc.status(), StatusCode::OK);
        let run_id = enc.headers().get("x-run-id").unwrap().to_str().unwrap();
        assert!(run_id.starts_with("r-"));
        let container = body(enc).await;
        assert_eq!(&container[0..4], b"HCMP");

        let ver = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/verify")
                    .body(Body::from(container.clone()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(ver.status(), StatusCode::OK);
        let v: serde_json::Value = serde_json::from_slice(&body(ver).await).unwrap();
        assert_eq!(v["ok"], true);
        assert!(v["info"]["block_count"].as_u64().unwrap() >= 1);

        let dec = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/decode")
                    .body(Body::from(container))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(dec.status(), StatusCode::OK);
        assert_eq!(body(dec).await, payload);
    }

    #[tokio::test]
    async fn decode_unknown_version_is_422_with_category() {
        let app = router(test_state().await);
        let mut bogus = vec![0u8; 16];
        bogus[0..4].copy_from_slice(b"HCMP");
        bogus[4] = 99;
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/decode")
                    .body(Body::from(bogus))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
        let v: serde_json::Value = serde_json::from_slice(&body(resp).await).unwrap();
        assert_eq!(v["ok"], false);
        assert_eq!(v["error"], "unknown_version");
    }

    #[tokio::test]
    async fn object_lifecycle_over_http() {
        let app = router(test_state().await);

        let create = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("PUT")
                    .uri("/api/v1/objects")
                    .header("x-object-id", "doc1")
                    .body(Body::from(b"hello object store".to_vec()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(create.status(), StatusCode::CREATED);

        let dup = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("PUT")
                    .uri("/api/v1/objects")
                    .header("x-object-id", "doc1")
                    .body(Body::from(b"x".to_vec()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(dup.status(), StatusCode::CONFLICT);

        let overwrite = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("PUT")
                    .uri("/api/v1/objects/doc1")
                    .body(Body::from(b"updated".to_vec()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(overwrite.status(), StatusCode::OK);

        let get = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/api/v1/objects/doc1")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(get.status(), StatusCode::OK);
        assert_eq!(body(get).await, b"updated");

        let verify = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/objects/doc1/verify")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(verify.status(), StatusCode::OK);

        let meta = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/api/v1/objects/doc1/meta")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(meta.status(), StatusCode::OK);

        let list = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/objects")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(list.status(), StatusCode::OK);
        let v: serde_json::Value = serde_json::from_slice(&body(list).await).unwrap();
        assert_eq!(v["count"], 1);

        let delete = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("DELETE")
                    .uri("/api/v1/objects/doc1")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(delete.status(), StatusCode::OK);
        let missing = app
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/api/v1/objects/doc1")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(missing.status(), StatusCode::NOT_FOUND);
    }
}
