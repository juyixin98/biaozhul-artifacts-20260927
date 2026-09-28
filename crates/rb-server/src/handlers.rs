//! HTTP 处理器与路由表。

use axum::body::to_bytes;
use axum::extract::{Json as AxJson, Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Extension, Json, Router};
use serde::Deserialize;
use tracing::{debug, info};

use rb_format::RoaringSet;

use crate::error::ApiError;
use crate::model::*;
use crate::request_id::ReqId;
use crate::state::AppState;

type ApiResult = Result<Response, ApiError>;

/// 路由（state 在 main 中注入）。
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/sets", get(list_sets).post(create_set))
        .route(
            "/v1/sets/:name",
            get(get_set).delete(delete_set).put(replace_set),
        )
        .route("/v1/sets/:name/values", get(values_page).post(add_values))
        .route("/v1/sets/:name/contains/:value", get(contains))
        .route("/v1/sets/:name/rank/:x", get(rank))
        .route("/v1/sets/:name/select/:i", get(select))
        .route("/v1/sets/:name/union", post(op_union))
        .route("/v1/sets/:name/intersect", post(op_intersect))
        .route("/v1/sets/:name/difference", post(op_difference))
}

/// 成功信封响应。
fn ok<T: serde::Serialize>(rid: &str, value: T) -> Response {
    (StatusCode::OK, Json(Envelope::ok(rid, value))).into_response()
}

fn validate(name: &str) -> Result<(), ApiError> {
    if rb_persist::naming::is_valid_name(name) {
        Ok(())
    } else {
        Err(ApiError::invalid_name(name))
    }
}

// ---------------- 健康检查 ----------------

async fn healthz(Extension(ReqId(rid)): Extension<ReqId>) -> Response {
    ok(
        &rid,
        serde_json::json!({
            "status": "ok",
            "format_version": format!("{:#010x}", rb_format::FORMAT_VERSION),
            "threshold": rb_format::ARRAY_MAX_CARDINALITY,
            "container_bits": rb_format::CONTAINER_BITS,
        }),
    )
}

// ---------------- 列表 / 创建 / 替换 / 删除 ----------------

#[derive(Debug, Deserialize)]
struct NameQuery {
    name: String,
}

async fn list_sets(
    State(state): State<AppState>,
    Extension(ReqId(rid)): Extension<ReqId>,
) -> ApiResult {
    let store = state.store.clone();
    let names = tokio::task::spawn_blocking(move || store.list())
        .await
        .expect("blocking list panicked")?;
    info!(request_id = %rid, count = names.len(), "list sets");
    Ok(ok(&rid, serde_json::json!({ "sets": names })))
}

/// POST /v1/sets?name=foo
async fn create_set(
    State(state): State<AppState>,
    Extension(ReqId(rid)): Extension<ReqId>,
    Query(q): Query<NameQuery>,
    AxJson(req): AxJson<CreateReq>,
) -> ApiResult {
    validate(&q.name)?;
    if req.expect_new {
        let store = state.store.clone();
        let n = q.name.clone();
        let exists = tokio::task::spawn_blocking(move || store.exists(&n))
            .await
            .expect("blocking exists panicked");
        if exists {
            return Err(ApiError::conflict(format!(
                "set already exists: {}",
                q.name
            )));
        }
    }
    let mut set = RoaringSet::new();
    let mut added = 0u64;
    for v in req.values {
        if set.insert(v) {
            added += 1;
        }
    }
    state.put(&q.name, set.clone()).await?;
    info!(request_id = %rid, name = %q.name, added, "created set");
    let env = Envelope::ok(rid.clone(), summary(&q.name, &set, true))
        .with_step("create", format!("{added} unique values inserted"))
        .with_note("duplicate values in the request were collapsed");
    Ok((StatusCode::CREATED, Json(env)).into_response())
}

/// PUT /v1/sets/:name —— 用给定成员整体替换（upsert）。
async fn replace_set(
    State(state): State<AppState>,
    Extension(ReqId(rid)): Extension<ReqId>,
    Path(name): Path<String>,
    AxJson(req): AxJson<ValuesReq>,
) -> ApiResult {
    validate(&name)?;
    let mut set = RoaringSet::new();
    for v in req.values {
        set.insert(v);
    }
    state.put(&name, set.clone()).await?;
    info!(request_id = %rid, name = %name, "replaced set");
    Ok(ok(
        &rid,
        serde_json::json!({ "name": name, "persisted": true, "cardinality": set.len_u64() }),
    ))
}

async fn delete_set(
    State(state): State<AppState>,
    Extension(ReqId(rid)): Extension<ReqId>,
    Path(name): Path<String>,
) -> ApiResult {
    validate(&name)?;
    let store = state.store.clone();
    let n = name.clone();
    let removed = tokio::task::spawn_blocking(move || store.delete(&n))
        .await
        .expect("blocking delete panicked")?;
    state.evict(&name).await;
    if !removed {
        return Err(ApiError::not_found(name));
    }
    info!(request_id = %rid, name = %name, "deleted set");
    Ok(ok(
        &rid,
        serde_json::json!({ "name": name, "deleted": true }),
    ))
}

async fn get_set(
    State(state): State<AppState>,
    Extension(ReqId(rid)): Extension<ReqId>,
    Path(name): Path<String>,
) -> ApiResult {
    validate(&name)?;
    let set = state.get_cached(&name).await?;
    Ok(ok(&rid, summary(&name, &set, false)))
}

// ---------------- 元素操作 ----------------

async fn add_values(
    State(state): State<AppState>,
    Extension(ReqId(rid)): Extension<ReqId>,
    Path(name): Path<String>,
    AxJson(req): AxJson<ValuesReq>,
) -> ApiResult {
    validate(&name)?;
    // 不存在则按空集合创建（POST 语义）。
    let mut set = match state.get_cached(&name).await {
        Ok(s) => (*s).clone(),
        Err(rb_persist::StoreError::NotFound(_)) => RoaringSet::new(),
        Err(e) => return Err(e.into()),
    };
    let mut added = 0u64;
    for v in &req.values {
        if set.insert(*v) {
            added += 1;
        }
    }
    state.put(&name, set.clone()).await?;
    debug!(
        request_id = %rid, name = %name,
        submitted = req.values.len(), newly_added = added,
        after = set.len_u64(), "values added"
    );
    let env = Envelope::ok(rid.clone(), summary(&name, &set, true))
        .with_step(
            "insert",
            format!("{} submitted, {added} newly inserted", req.values.len()),
        )
        .with_note("duplicate values within the request or existing set were collapsed");
    Ok((StatusCode::OK, Json(env)).into_response())
}

#[derive(Debug, Deserialize)]
struct ValuesPageQuery {
    #[serde(default)]
    limit: Option<usize>,
}

async fn values_page(
    State(state): State<AppState>,
    Extension(ReqId(rid)): Extension<ReqId>,
    Path(name): Path<String>,
    Query(q): Query<ValuesPageQuery>,
) -> ApiResult {
    validate(&name)?;
    let set = state.get_cached(&name).await?;
    let limit = q.limit.unwrap_or(100).min(10_000);
    let values: Vec<u32> = set.iter().take(limit).collect();
    let total = set.len_u64();
    let truncated = total > values.len() as u64;
    let mut env = Envelope::ok(
        rid.clone(),
        ValuesResult {
            values,
            truncated,
            limit,
        },
    );
    if truncated {
        env = env.with_note(format!(
            "result truncated to first {limit} of {total} elements; set operations remain exact"
        ));
    }
    Ok((StatusCode::OK, Json(env)).into_response())
}

async fn contains(
    State(state): State<AppState>,
    Extension(ReqId(rid)): Extension<ReqId>,
    Path((name, value)): Path<(String, u32)>,
) -> ApiResult {
    validate(&name)?;
    let set = state.get_cached(&name).await?;
    Ok(ok(
        &rid,
        ContainsResult {
            value,
            contains: set.contains(value),
        },
    ))
}

async fn rank(
    State(state): State<AppState>,
    Extension(ReqId(rid)): Extension<ReqId>,
    Path((name, x)): Path<(String, u32)>,
) -> ApiResult {
    validate(&name)?;
    let set = state.get_cached(&name).await?;
    let r = set.rank(x);
    let env = Envelope::ok(rid.clone(), RankResult { x, rank: r }).with_step(
        "rank",
        "count elements strictly less than x; summed over containers with u64 arithmetic",
    );
    Ok((StatusCode::OK, Json(env)).into_response())
}

async fn select(
    State(state): State<AppState>,
    Extension(ReqId(rid)): Extension<ReqId>,
    Path((name, i)): Path<(String, i64)>,
) -> ApiResult {
    validate(&name)?;
    if i < 0 {
        return Err(ApiError::bad_request("select index must be >= 0"));
    }
    let set = state.get_cached(&name).await?;
    let value = set.select(i as u64);
    let mut env = Envelope::ok(rid.clone(), SelectResult { i: i as u64, value });
    if value.is_none() {
        env = env.with_note(format!(
            "index {i} is >= cardinality {}; no such element",
            set.len_u64()
        ));
    }
    Ok((StatusCode::OK, Json(env)).into_response())
}

// ---------------- 集合运算（宏复用三个端点） ----------------

macro_rules! set_op {
    ($fn_name:ident, $method:ident, $stage:literal, $desc:literal, $sym:literal) => {
        async fn $fn_name(
            State(state): State<AppState>,
            Extension(ReqId(rid)): Extension<ReqId>,
            Path(name): Path<String>,
            AxJson(req): AxJson<OpReq>,
        ) -> ApiResult {
            validate(&name)?;
            validate(&req.with)?;
            let a = state.get_cached(&name).await?;
            let b = state.get_cached(&req.with).await?;

            let result = a.$method(&b);
            let result_name = format!("{name} {} {}", $sym, req.with);
            debug!(
                request_id = %rid, op = $stage, left = %name, right = %req.with,
                result_card = result.len_u64(), "set operation"
            );
            let env = Envelope::ok(rid.clone(), summary(&result_name, &result, false))
                .with_step(
                    "load:left",
                    format!(
                        "{name}: {} containers ({} array / {} bitmap)",
                        a.container_count(),
                        container_kinds(&a).array,
                        container_kinds(&a).bitmap
                    ),
                )
                .with_step(
                    "load:right",
                    format!(
                        "{}: {} containers ({} array / {} bitmap)",
                        req.with,
                        b.container_count(),
                        container_kinds(&b).array,
                        container_kinds(&b).bitmap
                    ),
                )
                .with_step($stage, $desc)
                .with_step(
                    "representation",
                    format!(
                        "result: {} containers ({} array / {} bitmap), {} elements",
                        result.container_count(),
                        container_kinds(&result).array,
                        container_kinds(&result).bitmap,
                        result.len_u64()
                    ),
                )
                .with_note(
                    "operation result is not persisted; computed on the native \
                     container representation (no full integer-set expansion)",
                );
            Ok((StatusCode::OK, Json(env)).into_response())
        }
    };
}

set_op!(
    op_union,
    union,
    "op:union",
    "bitwise OR for bitmap pairs, sorted merge for array pairs",
    "∪"
);
set_op!(
    op_intersect,
    intersect,
    "op:intersect",
    "bitwise AND for bitmap pairs, array-driven probes for mixed pairs",
    "∩"
);
set_op!(
    op_difference,
    difference,
    "op:difference",
    "bitwise AND-NOT for bitmap pairs, sorted merge for array pairs",
    "∖"
);

// ---------------- 汇总工具 ----------------

fn container_kinds(set: &RoaringSet) -> ContainerKinds {
    let mut array = 0;
    let mut bitmap = 0;
    for i in 0..set.container_count() {
        if set.container_at(i).is_array() {
            array += 1;
        } else {
            bitmap += 1;
        }
    }
    ContainerKinds { array, bitmap }
}

fn summary(name: &str, set: &RoaringSet, persisted: bool) -> SetSummary {
    SetSummary {
        name: name.to_string(),
        cardinality: set.len_u64(),
        containers: set.container_count(),
        container_kinds: container_kinds(set),
        min: set.min(),
        max: set.max(),
        persisted,
    }
}

/// 错误响应后处理：`?` 错误路径构造信封时没有请求 id，
/// 在请求 id 中间件层读取小响应体，把 `request_id` 补成与响应头一致的值。
/// 成功响应原样透传（不缓冲大结果体）。
pub async fn patch_error_id(id: String, resp: Response) -> Response {
    if resp.status().is_success() {
        return resp;
    }
    let is_json = resp
        .headers()
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|ct| ct.contains("application/json"));
    if !is_json {
        return resp;
    }
    let (mut parts, body) = resp.into_parts();
    let bytes = match to_bytes(body, 1 << 20).await {
        Ok(b) => b,
        Err(_) => return Response::from_parts(parts, axum::body::Body::empty()),
    };
    let mut value: serde_json::Value = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(_) => return Response::from_parts(parts, axum::body::Body::from(bytes)),
    };
    if value.get("errors").is_some() {
        value["request_id"] = serde_json::Value::String(id);
    }
    let new_bytes = serde_json::to_vec(&value).unwrap_or_else(|_| bytes.to_vec());
    parts.headers.insert(
        axum::http::header::CONTENT_LENGTH,
        axum::http::HeaderValue::from(new_bytes.len()),
    );
    Response::from_parts(parts, axum::body::Body::from(new_bytes))
}
