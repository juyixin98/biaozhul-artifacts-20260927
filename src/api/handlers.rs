//! HTTP handlers。重活（建索引/搜索/磁盘 I/O）一律 `spawn_blocking`，
//! 避免阻塞 Axum 异步运行时；所有错误都带请求运行编号返回。
//!
//! 提取器约定：业务入参用 `State/Path/Query/Json/Bytes`，
//! 运行编号用 `Extension<RunId>`（由中间件注入），不再直接取整个 Request。

use axum::{
    Extension, Json,
    body::Bytes,
    extract::{Path, Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde::Deserialize;

use super::types::{
    CountView, CreateIndexReq, DeletedView, HealthView, IndexList, IndexSource, ManifestView,
    SearchQuery, SearchReq, SearchView,
};
use super::{AppState, Envelope, InternalResponse, RunId};
use crate::error::FmError;

fn ok<T: serde::Serialize>(run: &RunId, code: StatusCode, data: T) -> Response {
    let mut resp = (
        code,
        Json(Envelope {
            run_id: run.0.clone(),
            data,
        }),
    )
        .into_response();
    resp.extensions_mut().insert(InternalResponse);
    resp
}

fn respond<T: serde::Serialize>(run: &RunId, result: crate::error::Result<T>) -> Response {
    match result {
        Ok(data) => ok(run, StatusCode::OK, data),
        Err(e) => e.into_api_response(&run.0),
    }
}

/// GET /v1/health
pub async fn health(State(svc): State<AppState>, run: Extension<RunId>) -> Response {
    ok(
        &run.0,
        StatusCode::OK,
        HealthView {
            status: "ok",
            loaded: svc.list_loaded(),
            on_disk: svc.list_on_disk(),
        },
    )
}

/// GET /v1/indexes
pub async fn list_indexes(State(svc): State<AppState>, run: Extension<RunId>) -> Response {
    ok(
        &run.0,
        StatusCode::OK,
        IndexList {
            loaded: svc.list_loaded(),
            on_disk: svc.list_on_disk(),
        },
    )
}

/// POST /v1/indexes
pub async fn create_index(
    State(svc): State<AppState>,
    run: Extension<RunId>,
    raw: Bytes,
) -> Response {
    let parsed: CreateIndexReq = match serde_json::from_slice(&raw) {
        Ok(v) => v,
        Err(e) => {
            return FmError::invalid_input(format!("请求体不是合法 JSON: {e}"))
                .into_api_response(&run.0.0);
        }
    };
    let name = parsed.name.clone();
    let rb = parsed.rank_block;
    let ss = parsed.sample_step;
    let source = match parsed.into_source() {
        Ok(s) => s,
        Err(e) => return e.into_api_response(&run.0.0),
    };

    let svc2 = svc.clone();
    let name_task = name.clone();
    let result = tokio::task::spawn_blocking(move || match source {
        IndexSource::Inline(bytes) => svc2.create_from_text(&name_task, bytes, rb, ss),
        IndexSource::File(rel) => svc2.create_from_file(&name_task, &rel, rb, ss),
    })
    .await;
    match result {
        Ok(Ok(manifest)) => ok(
            &run.0,
            StatusCode::CREATED,
            ManifestView::new(name, &manifest),
        ),
        Ok(Err(e)) => e.into_api_response(&run.0.0),
        Err(e) => FmError::computation_failed(format!("后台建索引任务失败: {e}"))
            .into_api_response(&run.0.0),
    }
}

/// GET /v1/indexes/{name}
pub async fn get_index(
    State(svc): State<AppState>,
    Path(name): Path<String>,
    run: Extension<RunId>,
) -> Response {
    respond(
        &run.0,
        svc.describe(&name).map(|m| ManifestView::new(name, &m)),
    )
}

/// DELETE /v1/indexes/{name}
pub async fn delete_index(
    State(svc): State<AppState>,
    Path(name): Path<String>,
    run: Extension<RunId>,
) -> Response {
    let svc2 = svc.clone();
    let name_task = name.clone();
    match tokio::task::spawn_blocking(move || svc2.delete(&name_task)).await {
        Ok(Ok(())) => ok(&run.0, StatusCode::OK, DeletedView { deleted: name }),
        Ok(Err(e)) => e.into_api_response(&run.0.0),
        Err(e) => {
            FmError::computation_failed(format!("后台任务失败: {e}")).into_api_response(&run.0.0)
        }
    }
}

/// POST /v1/indexes/{name}/load
pub async fn load_index(
    State(svc): State<AppState>,
    Path(name): Path<String>,
    run: Extension<RunId>,
) -> Response {
    let svc2 = svc.clone();
    let name_task = name.clone();
    match tokio::task::spawn_blocking(move || svc2.load(&name_task)).await {
        Ok(Ok(())) => respond(
            &run.0,
            svc.describe(&name).map(|m| ManifestView::new(name, &m)),
        ),
        Ok(Err(e)) => e.into_api_response(&run.0.0),
        Err(e) => {
            FmError::computation_failed(format!("后台任务失败: {e}")).into_api_response(&run.0.0)
        }
    }
}

/// GET /v1/indexes/{name}/search?pattern=...
pub async fn search_get(
    State(svc): State<AppState>,
    Path(name): Path<String>,
    Query(q): Query<SearchQuery>,
    run: Extension<RunId>,
) -> Response {
    let trace = q.trace;
    let pattern = match q.into_pattern() {
        Ok(p) => p,
        Err(e) => return e.into_api_response(&run.0.0),
    };
    run_search(svc, &name, pattern, trace, &run.0).await
}

/// POST /v1/indexes/{name}/search
pub async fn search_post(
    State(svc): State<AppState>,
    Path(name): Path<String>,
    run: Extension<RunId>,
    raw: Bytes,
) -> Response {
    let parsed: SearchReq = match serde_json::from_slice(&raw) {
        Ok(v) => v,
        Err(e) => {
            return FmError::invalid_input(format!("请求体不是合法 JSON: {e}"))
                .into_api_response(&run.0.0);
        }
    };
    let trace = parsed.trace;
    let pattern = match parsed.into_pattern() {
        Ok(p) => p,
        Err(e) => return e.into_api_response(&run.0.0),
    };
    run_search(svc, &name, pattern, trace, &run.0).await
}

async fn run_search(
    svc: AppState,
    name: &str,
    pattern: Vec<u8>,
    trace: bool,
    run: &RunId,
) -> Response {
    let svc2 = svc.clone();
    let name = name.to_string();
    let pattern_task = pattern.clone();
    match tokio::task::spawn_blocking(move || svc2.search(&name, &pattern_task, trace)).await {
        Ok(Ok(r)) => ok(run, StatusCode::OK, SearchView::from_result(&pattern, r)),
        Ok(Err(e)) => e.into_api_response(&run.0),
        Err(e) => {
            FmError::computation_failed(format!("后台查询任务失败: {e}")).into_api_response(&run.0)
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct CountParams {
    #[serde(default)]
    pattern: Option<String>,
    #[serde(default)]
    pattern_base64: Option<String>,
    #[serde(default)]
    empty: Option<String>,
}

/// GET /v1/indexes/{name}/count
pub async fn count_get(
    State(svc): State<AppState>,
    Path(name): Path<String>,
    Query(params): Query<CountParams>,
    run: Extension<RunId>,
) -> Response {
    let parsed = SearchReq {
        pattern: params.pattern,
        pattern_base64: params.pattern_base64,
        empty: matches!(params.empty.as_deref(), Some("true") | Some("1")),
        trace: false,
    };
    let pattern = match parsed.into_pattern() {
        Ok(p) => p,
        Err(e) => return e.into_api_response(&run.0.0),
    };
    let svc2 = svc.clone();
    let pattern_task = pattern.clone();
    match tokio::task::spawn_blocking(move || svc2.count(&name, &pattern_task)).await {
        Ok(Ok(c)) => ok(
            &run.0,
            StatusCode::OK,
            CountView {
                pattern_len: pattern.len() as u64,
                count: c,
            },
        ),
        Ok(Err(e)) => e.into_api_response(&run.0.0),
        Err(e) => FmError::computation_failed(format!("后台计数任务失败: {e}"))
            .into_api_response(&run.0.0),
    }
}

/// GET /v1/indexes/{name}/verify
pub async fn verify_get(
    State(svc): State<AppState>,
    Path(name): Path<String>,
    Query(q): Query<SearchQuery>,
    run: Extension<RunId>,
) -> Response {
    let pattern = match q.into_pattern() {
        Ok(p) => p,
        Err(e) => return e.into_api_response(&run.0.0),
    };
    run_verify(svc, &name, pattern, &run.0).await
}

/// POST /v1/indexes/{name}/verify
pub async fn verify_post(
    State(svc): State<AppState>,
    Path(name): Path<String>,
    run: Extension<RunId>,
    raw: Bytes,
) -> Response {
    let parsed: SearchReq = match serde_json::from_slice(&raw) {
        Ok(v) => v,
        Err(e) => {
            return FmError::invalid_input(format!("请求体不是合法 JSON: {e}"))
                .into_api_response(&run.0.0);
        }
    };
    let pattern = match parsed.into_pattern() {
        Ok(p) => p,
        Err(e) => return e.into_api_response(&run.0.0),
    };
    run_verify(svc, &name, pattern, &run.0).await
}

async fn run_verify(svc: AppState, name: &str, pattern: Vec<u8>, run: &RunId) -> Response {
    let svc2 = svc.clone();
    let name = name.to_string();
    match tokio::task::spawn_blocking(move || svc2.verify(&name, &pattern)).await {
        Ok(Ok(v)) => ok(run, StatusCode::OK, v),
        Ok(Err(e)) => e.into_api_response(&run.0),
        Err(e) => {
            FmError::computation_failed(format!("后台校验任务失败: {e}")).into_api_response(&run.0)
        }
    }
}
