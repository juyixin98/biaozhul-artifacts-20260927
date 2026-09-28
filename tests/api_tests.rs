//! In-process HTTP tests via tower `oneshot` (no network socket needed).
//!
//! Expected answers are hand-computed from the fixed fixture array, never
//! produced by the kernel under test. Error paths assert both the HTTP
//! status and the stable `error.kind` string.

use std::path::PathBuf;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;
use wavelet_matrix_service::api::AppState;
use wavelet_matrix_service::index::WmIndex;
use wavelet_matrix_service::store::IndexStore;

const MIXED: [i64; 8] = [5, -3, 7, 7, 0, -3, 42, 1];

struct TempDir(PathBuf);
impl TempDir {
    fn new() -> Self {
        let mut p = std::env::temp_dir();
        p.push(format!(
            "wm-api-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&p).unwrap();
        TempDir(p)
    }
}
impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

struct App {
    state: AppState,
    _dir: TempDir,
}

async fn spawn_app() -> App {
    let dir = TempDir::new();
    let store = IndexStore::new(&dir.0).unwrap();
    let state = AppState::new(store);
    App { state, _dir: dir }
}

async fn send(
    router: axum::Router,
    method: &str,
    uri: &str,
    body: Option<Value>,
    request_id: Option<&str>,
    content_type: Option<&str>,
) -> (StatusCode, Value) {
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(ct) = content_type {
        builder = builder.header("content-type", ct);
    }
    if let Some(rid) = request_id {
        builder = builder.header("x-request-id", rid);
    }
    let body = match body {
        Some(v) => Body::from(v.to_string()),
        None => Body::empty(),
    };
    let resp = router.oneshot(builder.body(body).unwrap()).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}

async fn post_json(
    router: axum::Router,
    uri: &str,
    body: Value,
    rid: Option<&str>,
) -> (StatusCode, Value) {
    send(
        router,
        "POST",
        uri,
        Some(body),
        rid,
        Some("application/json"),
    )
    .await
}

#[tokio::test]
async fn health_and_index_lifecycle() {
    let app = spawn_app().await;
    let r = app.state.build_router();

    let (status, health) = send(r.clone(), "GET", "/health", None, None, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(health["result"]["status"], "ok");
    assert_eq!(health["result"]["indexes_loaded"], 0);

    // create index
    let (status, created) = post_json(
        r.clone(),
        "/v1/indexes",
        json!({"name": "demo", "values": MIXED}),
        Some("rid-create"),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(created["request_id"], "rid-create");
    assert_eq!(created["result"]["name"], "demo");
    assert_eq!(created["result"]["len"], 8);
    assert_eq!(created["result"]["distinct"], 6);
    assert_eq!(created["result"]["height"], 3);
    assert_eq!(created["result"]["format_version"], 1);
    assert!(created["result"]["persisted_bytes"].as_u64().unwrap() > 0);

    // it must be on disk and independently reloadable
    let store = IndexStore::new(app.state.store.dir()).unwrap();
    let reloaded = store.load("demo").unwrap();
    assert_eq!(reloaded, WmIndex::build(&MIXED).unwrap());

    // list + get
    let (_, listed) = send(r.clone(), "GET", "/v1/indexes", None, None, None).await;
    assert_eq!(listed["result"]["indexes"][0]["name"], "demo");
    let (status, got) = send(r.clone(), "GET", "/v1/indexes/demo", None, None, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(got["result"]["distinct"], 6);
}

#[tokio::test]
async fn queries_return_hand_computed_values() {
    let app = spawn_app().await;
    let r = app.state.build_router();
    let (status, _) = post_json(
        r.clone(),
        "/v1/indexes",
        json!({"name": "demo", "values": MIXED}),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);

    let query = |body: Value| post_json(r.clone(), "/v1/indexes/demo/queries", body, Some("rid-q"));

    // sorted: [-3, -3, 0, 1, 5, 7, 7, 42]
    let (s, v) = query(json!({"op":"kth_smallest","l":0,"r":8,"k":0})).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["result"]["value"], -3);
    let (s, v) = query(json!({"op":"kth_smallest","l":0,"r":8,"k":3})).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["result"]["value"], 1);
    let (s, v) = query(json!({"op":"kth_smallest","l":0,"r":8,"k":7})).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["result"]["value"], 42);

    // sub-window [2,6) = [7,7,0,-3]
    let (s, v) = query(json!({"op":"kth_smallest","l":2,"r":6,"k":1})).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["result"]["value"], 0);

    let (s, v) = query(json!({"op":"count_lt","l":0,"r":8,"bound":0})).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["result"]["count"], 2);
    let (s, v) = query(json!({"op":"count_lt","l":0,"r":8,"bound":8})).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["result"]["count"], 7);

    let (s, v) = query(json!({"op":"count_range","l":0,"r":8,"lo":0,"hi":8})).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["result"]["count"], 5);

    let (s, v) = query(json!({"op":"predecessor","l":0,"r":8,"bound":7})).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["result"]["found"], true);
    assert_eq!(v["result"]["value"], 5);
    let (s, v) = query(json!({"op":"predecessor","l":0,"r":8,"bound":-3})).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["result"]["found"], false);
    assert!(v["result"]["value"].is_null());

    let (s, v) = query(json!({"op":"successor","l":0,"r":8,"bound":7})).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["result"]["value"], 7);
    let (s, v) = query(json!({"op":"successor","l":0,"r":8,"bound":43})).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["result"]["found"], false);

    // every result carries the request id
    assert_eq!(v["request_id"], "rid-q");
}

#[tokio::test]
async fn failure_paths_report_specific_kinds_and_statuses() {
    let app = spawn_app().await;
    let r = app.state.build_router();

    let create = |body: Value| post_json(r.clone(), "/v1/indexes", body, None);
    let (s, _v) = create(json!({"name":"demo","values":MIXED})).await;
    assert_eq!(s, StatusCode::CREATED);

    // duplicate index -> 409
    let (s, v) = create(json!({"name":"demo","values":[1,2]})).await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(v["error"]["kind"], "duplicate_index");
    assert_eq!(v["ok"], false);

    // empty input -> 400 empty_input
    let (s, v) = create(json!({"name":"empty","values":[]})).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["kind"], "empty_input");

    // invalid name -> 400 invalid_index_name
    let (s, v) = create(json!({"name":"../evil","values":[1]})).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["kind"], "invalid_index_name");

    let query = |body: Value| post_json(r.clone(), "/v1/indexes/demo/queries", body, None);

    // empty window -> 400 empty_range
    let (s, v) = query(json!({"op":"kth_smallest","l":3,"r":3,"k":0})).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["kind"], "empty_range");

    // window out of range -> 400 invalid_range
    let (s, v) = query(json!({"op":"kth_smallest","l":0,"r":9,"k":0})).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["kind"], "invalid_range");

    // k out of bounds -> 400 k_out_of_bounds
    let (s, v) = query(json!({"op":"kth_smallest","l":0,"r":8,"k":8})).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["kind"], "k_out_of_bounds");

    // missing k -> 400 bad_request
    let (s, v) = query(json!({"op":"kth_smallest","l":0,"r":8})).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["kind"], "bad_request");

    // missing bound -> 400 bad_request
    let (s, v) = query(json!({"op":"count_lt","l":0,"r":8})).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["kind"], "bad_request");

    // unknown op -> 400 bad_request
    let (s, v) = query(json!({"op":"median","l":0,"r":8})).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["kind"], "bad_request");

    // unknown index -> 404 index_not_found
    let (s, v) = post_json(
        r.clone(),
        "/v1/indexes/nope/queries",
        json!({"op":"count_lt","l":0,"r":1,"bound":0}),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    assert_eq!(v["error"]["kind"], "index_not_found");

    // malformed JSON -> 400 bad_request
    let (s, v) = send_raw(r.clone(), "POST", "/v1/indexes", "{ not json").await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"]["kind"], "bad_request");
}

#[tokio::test]
async fn request_id_is_echoed_or_generated() {
    let app = spawn_app().await;
    let r = app.state.build_router();
    let (_, v) = send(
        r.clone(),
        "GET",
        "/health",
        None,
        Some("my-trace-123"),
        None,
    )
    .await;
    assert_eq!(v["request_id"], "my-trace-123");

    let (_, v) = send(r.clone(), "GET", "/health", None, None, None).await;
    let rid = v["request_id"].as_str().unwrap();
    assert!(rid.starts_with("req-"), "got {rid}");
    assert!(rid.len() > "req-".len());
}

async fn send_raw(router: axum::Router, method: &str, uri: &str, raw: &str) -> (StatusCode, Value) {
    let req = Request::builder()
        .method(method)
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(raw.to_string()))
        .unwrap();
    let resp = router.oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}
