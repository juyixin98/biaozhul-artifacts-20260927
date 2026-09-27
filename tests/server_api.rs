//! HTTP interface tests driven through the real Axum router with
//! `tower::ServiceExt::oneshot` — no network sockets, no external tools.
//!
//! Each test asserts status codes and the concrete decision/reason fields.

mod common;

use axum::body::to_bytes;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use mphf::builder::BuildConfig;
use mphf::persistence::Store;
use mphf::server::{app, AppState};
use std::sync::Arc;
use tower::ServiceExt;

fn state(dir: &std::path::Path) -> AppState {
    AppState {
        store: Store::new(dir).unwrap(),
        defaults: Arc::new(BuildConfig::default()),
    }
}

async fn send(app: &axum::Router, req: Request<Body>) -> (StatusCode, serde_json::Value) {
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let json = serde_json::from_slice(&bytes).unwrap_or(serde_json::Value::Null);
    (status, json)
}

fn post(path: &str, body: serde_json::Value) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(path)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap()
}

fn get(path: &str) -> Request<Body> {
    Request::builder().uri(path).body(Body::empty()).unwrap()
}

#[tokio::test]
async fn build_then_member_accepted_and_nonmember_rejected() {
    let dir = tempfile::tempdir().unwrap();
    let a = app(state(dir.path()));

    let body = serde_json::json!({
        "name": "fruits",
        "keys": ["apple", "banana", "cherry", "date"],
        "fingerprint_bits": 0
    });
    let (st, j) = send(&a, post("/sets", body)).await;
    assert_eq!(st, StatusCode::CREATED, "{j}");
    assert_eq!(j["status"], "created");
    assert_eq!(j["n"], 4);
    assert_eq!(j["verify"], "full_key");
    assert!(j["seed"].is_number());
    assert!(j["attempts"].as_u64().unwrap() >= 1);

    // Member.
    let (st, j) = send(
        &a,
        post(
            "/sets/fruits/lookup",
            serde_json::json!({"key": "banana"}),
        ),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(j["member"], true);
    assert_eq!(j["decision"], "accept");
    assert_eq!(j["reason"], "verifier_match");
    let slot = j["slot"].as_u64().unwrap();
    assert!(slot < 4);

    // Non-member: rejected (reason varies by where its selector lands).
    let (st, j) = send(
        &a,
        post("/sets/fruits/lookup", serde_json::json!({"key": "durian"})),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(j["member"], false);
    assert_eq!(j["decision"], "reject");
    assert!(
        matches!(
            j["reason"].as_str().unwrap(),
            "key_mismatch" | "unoccupied_vertex" | "fingerprint_mismatch" | "edge_collision"
        ),
        "got {}",
        j["reason"]
    );
    assert!(j["request_id"].is_string());

    // Unknown set -> 404 with concrete error kind.
    let (st, j) = send(
        &a,
        post("/sets/ghost/lookup", serde_json::json!({"key": "x"})),
    )
    .await;
    assert_eq!(st, StatusCode::NOT_FOUND);
    assert_eq!(j["error"], "set_not_found");
}

#[tokio::test]
async fn request_id_is_echoed_from_header_else_generated() {
    let dir = tempfile::tempdir().unwrap();
    let a = app(state(dir.path()));
    let _ = send(
        &a,
        post(
            "/sets",
            serde_json::json!({"name": "r", "keys": ["k"], "fingerprint_bits": 0}),
        ),
    )
    .await;
    let req = Request::builder()
        .method("POST")
        .uri("/sets/r/lookup")
        .header("content-type", "application/json")
        .header("x-request-id", "trace-xyz-123")
        .body(Body::from(serde_json::json!({"key": "k"}).to_string()))
        .unwrap();
    let (_, j) = send(&a, req).await;
    assert_eq!(j["request_id"], "trace-xyz-123");

    // Without a header the server mints one.
    let (_, j) = send(
        &a,
        post("/sets/r/lookup", serde_json::json!({"key": "k"})),
    )
    .await;
    let rid = j["request_id"].as_str().unwrap();
    assert_eq!(rid.len(), 16);
    assert!(rid.chars().all(|c| c.is_ascii_hexdigit()));
}

#[tokio::test]
async fn duplicate_keys_are_deduplicated_and_counted() {
    let dir = tempfile::tempdir().unwrap();
    let a = app(state(dir.path()));
    let (st, j) = send(
        &a,
        post(
            "/sets",
            serde_json::json!({
                "name": "d",
                "keys": ["a", "b", "a", "b", "c", "a"],
                "fingerprint_bits": 0
            }),
        ),
    )
    .await;
    assert_eq!(st, StatusCode::CREATED, "{j}");
    assert_eq!(j["n"], 3);
    assert_eq!(j["duplicates_dropped"], 3);
}

#[tokio::test]
async fn empty_set_builds_and_rejects_all_lookups() {
    let dir = tempfile::tempdir().unwrap();
    let a = app(state(dir.path()));
    let (st, j) = send(
        &a,
        post("/sets", serde_json::json!({"name": "e", "keys": []})),
    )
    .await;
    assert_eq!(st, StatusCode::CREATED, "{j}");
    assert_eq!(j["n"], 0);

    let (st, j) = send(
        &a,
        post("/sets/e/lookup", serde_json::json!({"key": "anything"})),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(j["member"], false);
    assert_eq!(j["reason"], "empty_set");
}

#[tokio::test]
async fn bad_build_parameters_are_400_with_named_error() {
    let dir = tempfile::tempdir().unwrap();
    let a = app(state(dir.path()));
    // load factor outside the safe peel band.
    let (st, j) = send(
        &a,
        post(
            "/sets",
            serde_json::json!({"name": "x", "keys": ["a"], "load_factor": 2.5}),
        ),
    )
    .await;
    assert_eq!(st, StatusCode::BAD_REQUEST);
    assert_eq!(j["error"], "bad_request");

    // invalid fingerprint width.
    let (st, j) = send(
        &a,
        post(
            "/sets",
            serde_json::json!({"name": "y", "keys": ["a"], "fingerprint_bits": 13}),
        ),
    )
    .await;
    assert_eq!(st, StatusCode::BAD_REQUEST);
    assert_eq!(j["error"], "bad_request");

    // malformed JSON.
    let req = Request::builder()
        .method("POST")
        .uri("/sets")
        .header("content-type", "application/json")
        .body(Body::from("{ not json"))
        .unwrap();
    let (st, j) = send(&a, req).await;
    assert_eq!(st, StatusCode::BAD_REQUEST);
    assert_eq!(j["error"], "bad_request");
}

#[tokio::test]
async fn unsafe_set_names_are_rejected() {
    let dir = tempfile::tempdir().unwrap();
    let a = app(state(dir.path()));
    for bad in ["../escape", "/abs", "a/b", ".hidden", ""] {
        let body = serde_json::json!({"name": bad, "keys": ["x"]});
        let req = Request::builder()
            .method("POST")
            .uri("/sets")
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .unwrap();
        let (st, _) = send(&a, req).await;
        assert_eq!(st, StatusCode::BAD_REQUEST, "name {bad:?} should be rejected");
    }
}

#[tokio::test]
async fn fingerprint_mode_accepts_members_and_records_verifier() {
    let dir = tempfile::tempdir().unwrap();
    let a = app(state(dir.path()));
    let (st, j) = send(
        &a,
        post(
            "/sets",
            serde_json::json!({"name": "fp", "keys": ["one", "two"], "fingerprint_bits": 8}),
        ),
    )
    .await;
    assert_eq!(st, StatusCode::CREATED, "{j}");
    assert_eq!(j["verify"], "fingerprint_8");
    let (_, j) = send(
        &a,
        post("/sets/fp/lookup", serde_json::json!({"key": "one"})),
    )
    .await;
    assert_eq!(j["member"], true);
}

#[tokio::test]
async fn healthz_and_listing() {
    let dir = tempfile::tempdir().unwrap();
    let a = app(state(dir.path()));
    let (st, j) = send(&a, get("/healthz")).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(j["status"], "ok");
    assert_eq!(j["algorithm"], "bdz3-v1");

    send(
        &a,
        post(
            "/sets",
            serde_json::json!({"name": "z", "keys": ["q"], "fingerprint_bits": 0}),
        ),
    )
    .await;
    let (st, j) = send(&a, get("/sets")).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(j["sets"][0], "z");
}
