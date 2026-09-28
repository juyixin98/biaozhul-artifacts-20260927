//! Axum application: routes, request-id middleware and handlers.

use std::sync::Arc;

use axum::Router;
use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use hbs_config::Config;
use hbs_core::HierBitmap;
use hbs_format::FormatError;
use hbs_store::FileStore;
use hbs_testkit::props::cross_check;
use hbs_testkit::{Distribution, ReferenceSet};
use tower::ServiceBuilder;
use tower::util::MapRequestLayer;
use tracing::{debug, info, warn};

use crate::model::{Envelope, Json, parse_flat_json_object};

/// Format version surfaced in responses and logs.
pub const WIRE_FORMAT_VERSION: u16 = hbs_format::FORMAT_VERSION;

#[derive(Clone)]
pub struct AppState {
    pub store: Arc<FileStore>,
    pub config: Arc<Config>,
}

/// Build the router with the request-id layer applied to every route.
pub fn build_app(state: AppState) -> Router {
    let api = Router::new()
        .route("/health", get(health))
        .route("/sets", get(list_sets).post(create_set))
        .route("/sets/{name}", get(get_set).delete(delete_set))
        .route("/sets/{name}/rank", post(rank))
        .route("/sets/{name}/select", post(select))
        .route("/sets/{name}/contains", post(contains))
        .route("/sets/{name}/verify", get(verify_file))
        .route("/algebra/{op}", post(algebra))
        .route("/verify/cross-check", post(cross_check_handler))
        .route("/fixtures", post(fixtures_handler))
        .with_state(state);

    Router::new()
        .nest("/api/v1", api)
        .layer(ServiceBuilder::new().layer(MapRequestLayer::new(attach_request_id)))
}

/// Middleware: ensure every request has an id, honoring a client-provided
/// `X-Request-Id` when it is short and sane.
fn attach_request_id(
    mut req: axum::http::Request<axum::body::Body>,
) -> axum::http::Request<axum::body::Body> {
    let id = req
        .headers()
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .filter(|s| {
            !s.is_empty()
                && s.len() <= 64
                && s.chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        })
        .map(str::to_string)
        .unwrap_or_else(|| uuid::Uuid::new_v4().simple().to_string());
    req.extensions_mut().insert(RequestId(id));
    req
}

#[derive(Clone, Debug)]
struct RequestId(String);

fn request_id(headers: &HeaderMap, ext: &axum::http::Extensions) -> String {
    ext.get::<RequestId>()
        .map(|r| r.0.clone())
        .or_else(|| {
            headers
                .get("x-request-id")
                .and_then(|v| v.to_str().ok())
                .map(str::to_string)
        })
        .unwrap_or_else(|| "unknown".to_string())
}

// ---------------------------------------------------------------------------
// Response helpers
// ---------------------------------------------------------------------------

struct JsonResponse(StatusCode, String);

impl IntoResponse for JsonResponse {
    fn into_response(self) -> Response {
        Response::builder()
            .status(self.0)
            .header("content-type", "application/json")
            .body(axum::body::Body::from(self.1))
            .unwrap()
    }
}

fn ok(env: Envelope) -> JsonResponse {
    JsonResponse(StatusCode::OK, env.to_json())
}

fn fail(status: StatusCode, env: Envelope) -> JsonResponse {
    JsonResponse(status, env.to_json())
}

// ---------------------------------------------------------------------------
// Body parsing
// ---------------------------------------------------------------------------

struct ParsedBody {
    fields: Vec<(String, Json)>,
}

impl ParsedBody {
    fn get(&self, key: &str) -> Option<&Json> {
        self.fields.iter().find(|(k, _)| k == key).map(|(_, v)| v)
    }

    fn u32_array(&self, key: &str) -> Option<Vec<u32>> {
        match self.get(key)? {
            Json::Array(items) => {
                let mut out = Vec::with_capacity(items.len());
                for it in items {
                    match it {
                        Json::UInt(n) if *n <= u32::MAX as u64 => out.push(*n as u32),
                        _ => return None,
                    }
                }
                Some(out)
            }
            _ => None,
        }
    }

    fn u32_scalar(&self, key: &str) -> Option<u32> {
        match self.get(key)? {
            Json::UInt(n) if *n <= u32::MAX as u64 => Some(*n as u32),
            _ => None,
        }
    }

    fn u64_scalar(&self, key: &str) -> Option<u64> {
        match self.get(key)? {
            Json::UInt(n) => Some(*n),
            _ => None,
        }
    }

    fn string(&self, key: &str) -> Option<&str> {
        match self.get(key)? {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }
}

async fn read_json(
    headers: &HeaderMap,
    max_bytes: usize,
    body: Bytes,
) -> Result<ParsedBody, (StatusCode, String)> {
    if body.len() > max_bytes {
        return Err((
            StatusCode::PAYLOAD_TOO_LARGE,
            format!("body of {} bytes exceeds limit {max_bytes}", body.len()),
        ));
    }
    // Content-Type is advisory; we validate the body itself either way.
    let _ = headers;
    parse_flat_json_object(std::str::from_utf8(&body).map_err(|_| {
        (
            StatusCode::BAD_REQUEST,
            "body is not valid UTF-8".to_string(),
        )
    })?)
    .map(|fields| ParsedBody { fields })
    .map_err(|e| (StatusCode::BAD_REQUEST, e))
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

async fn health(headers: HeaderMap, ext: axum::http::Extensions) -> JsonResponse {
    let rid = request_id(&headers, &ext);
    let mut env = Envelope::success(rid).step("health", "service up");
    env.put("format_version", Json::u(WIRE_FORMAT_VERSION as u64));
    ok(env)
}

async fn list_sets(
    State(state): State<AppState>,
    headers: HeaderMap,
    ext: axum::http::Extensions,
) -> JsonResponse {
    let rid = request_id(&headers, &ext);
    match state.store.list() {
        Ok(infos) => {
            let arr = Json::arr(infos.iter().map(|i| {
                Json::obj(vec![
                    ("name", Json::str(i.name.clone())),
                    ("file_size", Json::u(i.file_size)),
                    ("modified_unix", Json::Int(i.modified_unix)),
                ])
            }));
            let mut env = Envelope::success(rid.clone())
                .step("list_sets", format!("found {} set file(s)", infos.len()));
            env.put("sets", arr);
            info!(request_id = %rid, count = infos.len(), "list_sets");
            ok(env)
        }
        Err(e) => fail(
            StatusCode::INTERNAL_SERVER_ERROR,
            Envelope::error(rid, "store_io", e.to_string())
                .step("list_sets", "filesystem listing failed"),
        ),
    }
}

async fn create_set(
    State(state): State<AppState>,
    headers: HeaderMap,
    ext: axum::http::Extensions,
    body: Bytes,
) -> JsonResponse {
    let rid = request_id(&headers, &ext);
    let parsed = match read_json(&headers, state.config.max_request_bytes, body).await {
        Ok(p) => p,
        Err((status, msg)) => {
            return fail(status, Envelope::error(rid, "bad_request", msg));
        }
    };
    let name = match parsed.string("name") {
        Some(n) => n.to_string(),
        None => {
            return fail(
                StatusCode::BAD_REQUEST,
                Envelope::error(rid, "bad_request", "missing string field `name`"),
            );
        }
    };
    let values = parsed.u32_array("values").unwrap_or_default();
    debug!(request_id = %rid, %name, n = values.len(), "create_set");

    let mut set = HierBitmap::new();
    let mut inserted = 0u64;
    for v in &values {
        if set.insert(*v) {
            inserted += 1;
        }
    }
    let stats = set.stats();
    match state.store.create(&name, &set) {
        Ok(()) => {
            let mut env = Envelope::success(rid.clone())
                .step("build_set", format!("inserted {inserted} unique value(s)"))
                .step("persist", "fsynced temp file then renamed into place");
            env.put("name", Json::str(name));
            env.put("cardinality", Json::u(set.len()));
            env.put(
                "stats",
                Json::obj(vec![
                    ("chunks", Json::u(stats.chunks as u64)),
                    ("array_containers", Json::u(stats.array_containers as u64)),
                    ("bitmap_containers", Json::u(stats.bitmap_containers as u64)),
                ]),
            );
            info!(request_id = %rid, cardinality = set.len(), "create_set ok");
            ok(env)
        }
        Err(hbs_store::StoreError::AlreadyExists(_)) => fail(
            StatusCode::CONFLICT,
            Envelope::error(
                rid,
                "already_exists",
                format!("set {name:?} already exists"),
            ),
        ),
        Err(hbs_store::StoreError::BadName(_)) => fail(
            StatusCode::BAD_REQUEST,
            Envelope::error(rid, "bad_name", "name must be [A-Za-z0-9_-]{1,64}"),
        ),
        Err(e) => fail(
            StatusCode::INTERNAL_SERVER_ERROR,
            Envelope::error(rid, "store_io", e.to_string()),
        ),
    }
}

async fn get_set(
    State(state): State<AppState>,
    headers: HeaderMap,
    ext: axum::http::Extensions,
    Path(name): Path<String>,
) -> JsonResponse {
    let rid = request_id(&headers, &ext);
    load_set(&state, &rid, &name)
}

fn load_set(state: &AppState, rid: &str, name: &str) -> JsonResponse {
    match state.store.load(name) {
        Ok(set) => {
            let stats = set.stats();
            let sample: Vec<Json> = set.iter().take(100).map(|v| Json::u(v as u64)).collect();
            let mut env = Envelope::success(rid.to_string())
                .step("load_set", format!("decoded and validated {}.hbs", name));
            env.put("name", Json::str(name));
            env.put("cardinality", Json::u(set.len()));
            env.put("truncated_sample", Json::Array(sample));
            env.put("sample_is_truncated", Json::Bool(set.len() > 100));
            env.put(
                "stats",
                Json::obj(vec![
                    ("chunks", Json::u(stats.chunks as u64)),
                    ("array_containers", Json::u(stats.array_containers as u64)),
                    ("bitmap_containers", Json::u(stats.bitmap_containers as u64)),
                ]),
            );
            ok(env)
        }
        Err(hbs_store::StoreError::NotFound(_)) => fail(
            StatusCode::NOT_FOUND,
            Envelope::error(rid, "not_found", format!("set {name:?} not found")),
        ),
        Err(hbs_store::StoreError::BadName(_)) => fail(
            StatusCode::BAD_REQUEST,
            Envelope::error(rid, "bad_name", "name must be [A-Za-z0-9_-]{1,64}"),
        ),
        Err(hbs_store::StoreError::Corrupt(e)) => corrupt_response(rid, name, &e),
        Err(e) => fail(
            StatusCode::INTERNAL_SERVER_ERROR,
            Envelope::error(rid, "store_io", e.to_string()),
        ),
    }
}

async fn delete_set(
    State(state): State<AppState>,
    headers: HeaderMap,
    ext: axum::http::Extensions,
    Path(name): Path<String>,
) -> JsonResponse {
    let rid = request_id(&headers, &ext);
    match state.store.delete(&name) {
        Ok(()) => {
            let mut env =
                Envelope::success(rid.clone()).step("delete", format!("removed {name}.hbs"));
            env.put("deleted", Json::Bool(true));
            ok(env)
        }
        Err(hbs_store::StoreError::NotFound(_)) => fail(
            StatusCode::NOT_FOUND,
            Envelope::error(rid, "not_found", format!("set {name:?} not found")),
        ),
        Err(hbs_store::StoreError::BadName(_)) => fail(
            StatusCode::BAD_REQUEST,
            Envelope::error(rid, "bad_name", "name must be [A-Za-z0-9_-]{1,64}"),
        ),
        Err(e) => fail(
            StatusCode::INTERNAL_SERVER_ERROR,
            Envelope::error(rid, "store_io", e.to_string()),
        ),
    }
}

/// Map a decode failure to a 422 with the precise category.
fn corrupt_response(rid: &str, name: &str, e: &FormatError) -> JsonResponse {
    let code = match e {
        FormatError::Truncated => "corrupt_truncated",
        FormatError::BadMagic => "corrupt_magic",
        FormatError::UnsupportedVersion { .. } => "corrupt_version",
        FormatError::BadFlags(_) => "corrupt_flags",
        FormatError::BadHeader(_) => "corrupt_header",
        FormatError::ChecksumMismatch { .. } => "corrupt_checksum",
        FormatError::BadDirectory(_) => "corrupt_directory",
        FormatError::UnknownContainerKind(_) => "corrupt_container_kind",
        FormatError::Cardinality { .. } => "corrupt_cardinality",
        FormatError::ArrayNotSorted { .. } => "corrupt_array_order",
        FormatError::TrailingBytes => "corrupt_trailing_bytes",
    };
    warn!(request_id = %rid, set = %name, error = %e, "corrupt file rejected");
    fail(
        StatusCode::UNPROCESSABLE_ENTITY,
        Envelope::error(rid, code, e.to_string()).step(
            "decode",
            "full validation rejected the file before any set was built",
        ),
    )
}

// -- query handlers ----------------------------------------------------------

async fn rank(
    State(state): State<AppState>,
    headers: HeaderMap,
    ext: axum::http::Extensions,
    Path(name): Path<String>,
    body: Bytes,
) -> JsonResponse {
    let rid = request_id(&headers, &ext);
    let parsed = match read_json(&headers, state.config.max_request_bytes, body).await {
        Ok(p) => p,
        Err((s, m)) => return fail(s, Envelope::error(rid, "bad_request", m)),
    };
    let value = match parsed.u32_scalar("value") {
        Some(v) => v,
        None => {
            return fail(
                StatusCode::BAD_REQUEST,
                Envelope::error(rid, "bad_request", "missing integer field `value`"),
            );
        }
    };
    let inclusive = match parsed.get("inclusive") {
        Some(Json::Bool(b)) => *b,
        None => false,
        _ => {
            return fail(
                StatusCode::BAD_REQUEST,
                Envelope::error(rid, "bad_request", "`inclusive` must be boolean"),
            );
        }
    };
    let set = match state.store.load(&name) {
        Ok(s) => s,
        Err(hbs_store::StoreError::NotFound(_)) => {
            return fail(
                StatusCode::NOT_FOUND,
                Envelope::error(rid, "not_found", format!("set {name:?} not found")),
            );
        }
        Err(hbs_store::StoreError::BadName(_)) => {
            return fail(
                StatusCode::BAD_REQUEST,
                Envelope::error(rid, "bad_name", "name must be [A-Za-z0-9_-]{1,64}"),
            );
        }
        Err(hbs_store::StoreError::Corrupt(e)) => return corrupt_response(&rid, &name, &e),
        Err(e) => {
            return fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                Envelope::error(rid, "store_io", e.to_string()),
            );
        }
    };
    let result = if inclusive {
        set.rank_le(value)
    } else {
        set.rank_lt(value)
    };
    let mut env = Envelope::success(rid.clone()).step(
        "rank",
        format!(
            "rank over chunks before key {} plus within-chunk prefix",
            value >> 16
        ),
    );
    env.put("name", Json::str(name));
    env.put("value", Json::u(value as u64));
    env.put("inclusive", Json::Bool(inclusive));
    env.put("rank", Json::u(result));
    info!(request_id = %rid, value, inclusive, rank = result, "rank");
    ok(env)
}

async fn select(
    State(state): State<AppState>,
    headers: HeaderMap,
    ext: axum::http::Extensions,
    Path(name): Path<String>,
    body: Bytes,
) -> JsonResponse {
    let rid = request_id(&headers, &ext);
    let parsed = match read_json(&headers, state.config.max_request_bytes, body).await {
        Ok(p) => p,
        Err((s, m)) => return fail(s, Envelope::error(rid, "bad_request", m)),
    };
    let rankv = match parsed.u64_scalar("rank") {
        Some(r) => r,
        None => {
            return fail(
                StatusCode::BAD_REQUEST,
                Envelope::error(
                    rid,
                    "bad_request",
                    "missing non-negative integer field `rank`",
                ),
            );
        }
    };
    let set = match state.store.load(&name) {
        Ok(s) => s,
        Err(hbs_store::StoreError::NotFound(_)) => {
            return fail(
                StatusCode::NOT_FOUND,
                Envelope::error(rid, "not_found", format!("set {name:?} not found")),
            );
        }
        Err(hbs_store::StoreError::BadName(_)) => {
            return fail(
                StatusCode::BAD_REQUEST,
                Envelope::error(rid, "bad_name", "name must be [A-Za-z0-9_-]{1,64}"),
            );
        }
        Err(hbs_store::StoreError::Corrupt(e)) => return corrupt_response(&rid, &name, &e),
        Err(e) => {
            return fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                Envelope::error(rid, "store_io", e.to_string()),
            );
        }
    };
    let mut env = Envelope::success(rid.clone()).step(
        "select",
        "walked chunk cardinalities, then bit select in target word",
    );
    env.put("name", Json::str(name));
    env.put("rank", Json::u(rankv));
    match set.select(rankv) {
        Some(v) => {
            env.put("found", Json::Bool(true));
            env.put("value", Json::u(v as u64));
            ok(env)
        }
        None => {
            env.put("found", Json::Bool(false));
            env.uncertain_note("rank is at or past cardinality; no member exists at that rank");
            ok(env)
        }
    }
}

async fn contains(
    State(state): State<AppState>,
    headers: HeaderMap,
    ext: axum::http::Extensions,
    Path(name): Path<String>,
    body: Bytes,
) -> JsonResponse {
    let rid = request_id(&headers, &ext);
    let parsed = match read_json(&headers, state.config.max_request_bytes, body).await {
        Ok(p) => p,
        Err((s, m)) => return fail(s, Envelope::error(rid, "bad_request", m)),
    };
    let Some(value) = parsed.u32_scalar("value") else {
        return fail(
            StatusCode::BAD_REQUEST,
            Envelope::error(rid, "bad_request", "missing integer field `value`"),
        );
    };
    let set = match state.store.load(&name) {
        Ok(s) => s,
        Err(hbs_store::StoreError::NotFound(_)) => {
            return fail(
                StatusCode::NOT_FOUND,
                Envelope::error(rid, "not_found", format!("set {name:?} not found")),
            );
        }
        Err(hbs_store::StoreError::BadName(_)) => {
            return fail(
                StatusCode::BAD_REQUEST,
                Envelope::error(rid, "bad_name", "name must be [A-Za-z0-9_-]{1,64}"),
            );
        }
        Err(hbs_store::StoreError::Corrupt(e)) => return corrupt_response(&rid, &name, &e),
        Err(e) => {
            return fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                Envelope::error(rid, "store_io", e.to_string()),
            );
        }
    };
    let present = set.contains(value);
    let mut env = Envelope::success(rid.clone()).step(
        "contains",
        format!("chunk key {}, then binary search or bit test", value >> 16),
    );
    env.put("name", Json::str(name));
    env.put("value", Json::u(value as u64));
    env.put("present", Json::Bool(present));
    ok(env)
}

async fn verify_file(
    State(state): State<AppState>,
    headers: HeaderMap,
    ext: axum::http::Extensions,
    Path(name): Path<String>,
) -> JsonResponse {
    let rid = request_id(&headers, &ext);
    match state.store.verify(&name) {
        Ok(decoded) => {
            let mut env = Envelope::success(rid.clone()).step(
                "verify",
                format!(
                    "checksum, directory, offsets, kind and cardinality all valid \
                     (format v{})",
                    hbs_format::FORMAT_VERSION
                ),
            );
            env.put("name", Json::str(name));
            env.put("num_chunks", Json::u(decoded.num_chunks as u64));
            env.put("array_containers", Json::u(decoded.array_containers as u64));
            env.put(
                "bitmap_containers",
                Json::u(decoded.bitmap_containers as u64),
            );
            env.put("file_size", Json::u(decoded.encoded_len as u64));
            env.put("cardinality", Json::u(decoded.set.len()));
            ok(env)
        }
        Err(hbs_store::StoreError::NotFound(_)) => fail(
            StatusCode::NOT_FOUND,
            Envelope::error(rid, "not_found", format!("set {name:?} not found")),
        ),
        Err(hbs_store::StoreError::BadName(_)) => fail(
            StatusCode::BAD_REQUEST,
            Envelope::error(rid, "bad_name", "name must be [A-Za-z0-9_-]{1,64}"),
        ),
        Err(hbs_store::StoreError::Corrupt(e)) => corrupt_response(&rid, &name, &e),
        Err(e) => fail(
            StatusCode::INTERNAL_SERVER_ERROR,
            Envelope::error(rid, "store_io", e.to_string()),
        ),
    }
}

// -- algebra -----------------------------------------------------------------

async fn algebra(
    State(state): State<AppState>,
    headers: HeaderMap,
    ext: axum::http::Extensions,
    Path(op): Path<String>,
    body: Bytes,
) -> JsonResponse {
    let rid = request_id(&headers, &ext);
    let parsed = match read_json(&headers, state.config.max_request_bytes, body).await {
        Ok(p) => p,
        Err((s, m)) => return fail(s, Envelope::error(rid, "bad_request", m)),
    };
    let left = match parsed.string("left") {
        Some(n) => n.to_string(),
        None => {
            return fail(
                StatusCode::BAD_REQUEST,
                Envelope::error(rid, "bad_request", "missing string field `left`"),
            );
        }
    };
    let right = match parsed.string("right") {
        Some(n) => n.to_string(),
        None => {
            return fail(
                StatusCode::BAD_REQUEST,
                Envelope::error(rid, "bad_request", "missing string field `right`"),
            );
        }
    };
    let save_as = parsed.string("save_as").map(str::to_string);

    let a = match state.store.load(&left) {
        Ok(s) => s,
        Err(hbs_store::StoreError::NotFound(_)) => {
            return fail(
                StatusCode::NOT_FOUND,
                Envelope::error(rid, "not_found", format!("set {left:?} not found")),
            );
        }
        Err(hbs_store::StoreError::Corrupt(e)) => return corrupt_response(&rid, &left, &e),
        Err(e) => {
            return fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                Envelope::error(rid, "store_io", e.to_string()),
            );
        }
    };
    let b = match state.store.load(&right) {
        Ok(s) => s,
        Err(hbs_store::StoreError::NotFound(_)) => {
            return fail(
                StatusCode::NOT_FOUND,
                Envelope::error(rid, "not_found", format!("set {right:?} not found")),
            );
        }
        Err(hbs_store::StoreError::Corrupt(e)) => return corrupt_response(&rid, &right, &e),
        Err(e) => {
            return fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                Envelope::error(rid, "store_io", e.to_string()),
            );
        }
    };

    let result = match op.as_str() {
        "union" => a.union(&b),
        "intersection" => a.intersection(&b),
        "difference" => a.difference(&b),
        "symmetric-difference" | "symmetric_difference" => a.symmetric_difference(&b),
        other => {
            return fail(
                StatusCode::BAD_REQUEST,
                Envelope::error(
                    rid,
                    "unknown_op",
                    format!(
                        "unknown operation {other:?}; expected union|intersection|difference|symmetric-difference"
                    ),
                ),
            );
        }
    };
    let stats = result.stats();
    let op_label = op.clone();
    let mut env = Envelope::success(rid.clone()).step(
        "algebra",
        format!(
            "{op_label} performed chunk-wise ({} chunk(s) in result); no full-set expansion",
            result.chunk_count()
        ),
    );
    env.put("operation", Json::str(op_label.clone()));
    env.put("cardinality", Json::u(result.len()));
    env.put(
        "stats",
        Json::obj(vec![
            ("chunks", Json::u(stats.chunks as u64)),
            ("array_containers", Json::u(stats.array_containers as u64)),
            ("bitmap_containers", Json::u(stats.bitmap_containers as u64)),
        ]),
    );

    if let Some(target) = save_as {
        match state.store.save(&target, &result) {
            Ok(()) => {
                env.steps.push(crate::model::Step {
                    stage: "persist".to_string(),
                    detail: format!("result saved as {target:?}"),
                });
                env.put("saved_as", Json::str(target));
            }
            Err(hbs_store::StoreError::BadName(_)) => {
                return fail(
                    StatusCode::BAD_REQUEST,
                    Envelope::error(rid, "bad_name", "save_as must be [A-Za-z0-9_-]{1,64}"),
                );
            }
            Err(e) => {
                return fail(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Envelope::error(rid, "store_io", e.to_string()),
                );
            }
        }
    } else {
        env.uncertain_note("result not persisted: no `save_as` field was supplied");
    }
    info!(request_id = %rid, op = %op_label, cardinality = result.len(), "algebra ok");
    ok(env)
}

// -- cross-check (independent oracle) ---------------------------------------

async fn cross_check_handler(
    State(state): State<AppState>,
    headers: HeaderMap,
    ext: axum::http::Extensions,
    body: Bytes,
) -> JsonResponse {
    let rid = request_id(&headers, &ext);
    let parsed = match read_json(&headers, state.config.max_request_bytes, body).await {
        Ok(p) => p,
        Err((s, m)) => return fail(s, Envelope::error(rid, "bad_request", m)),
    };
    let a_values = match parsed.u32_array("a") {
        Some(v) => v,
        None => {
            return fail(
                StatusCode::BAD_REQUEST,
                Envelope::error(rid, "bad_request", "missing integer array field `a`"),
            );
        }
    };
    let b_values = parsed.u32_array("b").unwrap_or_default();

    let report = cross_check(&a_values, &b_values);
    let mut env = Envelope::success(rid.clone()).step(
        "oracle_cross_check",
        format!(
            "ran {} assertion(s) against an independent BTreeSet oracle",
            report.checks
        ),
    );
    env.put("checks", Json::u(report.checks as u64));
    match &report.failure {
        None => {
            env.put("passed", Json::Bool(true));
            info!(request_id = %rid, checks = report.checks, "cross-check passed");
        }
        Some(f) => {
            env.put("passed", Json::Bool(false));
            env.put(
                "failure",
                Json::obj(vec![
                    ("property", Json::str(f.property.clone())),
                    ("detail", Json::str(f.detail.clone())),
                ]),
            );
            warn!(request_id = %rid, property = %f.property, "cross-check discrepancy");
        }
    }
    ok(env)
}

async fn fixtures_handler(
    headers: HeaderMap,
    ext: axum::http::Extensions,
    State(state): State<AppState>,
    body: Bytes,
) -> JsonResponse {
    let rid = request_id(&headers, &ext);
    let parsed = match read_json(&headers, state.config.max_request_bytes, body).await {
        Ok(p) => p,
        Err((s, m)) => return fail(s, Envelope::error(rid, "bad_request", m)),
    };
    let distribution = parsed.string("distribution").unwrap_or("all");

    // Run cross-checks over the named distributions; no files are written by
    // this endpoint (use the CLI for fixture generation).
    let seeds = [1u64, 42, 777];
    let dists: Vec<Distribution> = match distribution {
        "all" => vec![
            Distribution::Sparse,
            Distribution::Dense,
            Distribution::Interleaved,
            Distribution::ContainerThreshold,
        ],
        "sparse" => vec![Distribution::Sparse],
        "dense" => vec![Distribution::Dense],
        "interleaved" => vec![Distribution::Interleaved],
        "container_threshold" | "threshold" => vec![Distribution::ContainerThreshold],
        other => {
            return fail(
                StatusCode::BAD_REQUEST,
                Envelope::error(
                    rid,
                    "unknown_distribution",
                    format!("{other:?}; expected sparse|dense|interleaved|container_threshold|all"),
                ),
            );
        }
    };

    let mut results = Vec::new();
    let mut total_checks = 0u64;
    for d in &dists {
        for seed in seeds {
            let s1 = hbs_testkit::sample(*d, seed);
            let s2 = hbs_testkit::sample(*d, seed.wrapping_add(9999));
            let report = cross_check(&s1.values, &s2.values);
            total_checks += report.checks as u64;
            let entry = Json::obj(vec![
                ("label", Json::str(s1.label.clone())),
                ("cardinality", Json::u(s1.values.len() as u64)),
                ("passed", Json::Bool(report.is_ok())),
                (
                    "failure",
                    match &report.failure {
                        None => Json::Null,
                        Some(f) => Json::obj(vec![
                            ("property", Json::str(f.property.clone())),
                            ("detail", Json::str(f.detail.clone())),
                        ]),
                    },
                ),
            ]);
            results.push(entry);
        }
    }
    let all_passed = results.iter().all(|j| {
        if let Json::Object(fields) = j {
            fields
                .iter()
                .any(|(k, v)| k == "passed" && matches!(v, Json::Bool(true)))
        } else {
            false
        }
    });
    let mut env = Envelope::success(rid.clone()).step(
        "fixtures",
        format!("cross-checked deterministic fixtures; {total_checks} oracle assertions"),
    );
    env.put("passed", Json::Bool(all_passed));
    env.put("results", Json::Array(results));
    info!(request_id = %rid, all_passed, "fixture verification");
    ok(env)
}

// ---------------------------------------------------------------------------
// CORS preflight for local tooling
// ---------------------------------------------------------------------------

#[allow(dead_code)]
fn method_allowed(m: &Method) -> bool {
    matches!(
        m,
        &Method::GET | &Method::POST | &Method::DELETE | &Method::OPTIONS
    )
}

// Builder-style helpers for the response envelope.
trait EnvelopeUncertain {
    fn uncertain_note(&mut self, note: impl Into<String>);
}

impl EnvelopeUncertain for Envelope {
    fn uncertain_note(&mut self, note: impl Into<String>) {
        self.uncertainties.push(note.into());
    }
}

/// Unused re-export kept to document the oracle dependency.
pub fn reference_of(values: &[u32]) -> ReferenceSet {
    ReferenceSet::from_values(values.iter().copied())
}
