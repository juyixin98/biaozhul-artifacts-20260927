//! End-to-end HTTP tests over the real Axum router (no network socket):
//! concrete status codes, failure categories, correlation ids, and the
//! redaction of sensitive labels are all asserted.

mod common;

use std::sync::Arc;

use bdd_backend::api::{app, AppState};
use bdd_backend::config::Config;
use serde_json::{json, Value};
use tower::ServiceExt;

async fn post(router: &axum::Router, uri: &str, body: Value) -> (axum::http::StatusCode, Value) {
    let resp = router
        .clone()
        .oneshot(
            axum::http::Request::builder()
                .method("POST")
                .uri(uri)
                .header("content-type", "application/json")
                .body(axum::body::Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    let json = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, json)
}

async fn get(router: &axum::Router, uri: &str) -> (axum::http::StatusCode, Value) {
    let resp = router
        .clone()
        .oneshot(
            axum::http::Request::builder()
                .uri(uri)
                .body(axum::body::Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    let json = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, json)
}

fn test_router() -> axum::Router {
    app(Arc::new(AppState::new(Config {
        max_truth_table_rows: 1 << 16,
        ..Config::default()
    })))
}

fn node_ref(v: &Value) -> Value {
    v["ref"].clone()
}

#[tokio::test]
async fn healthz_reports_ok() {
    let (status, body) = get(&test_router(), "/healthz").await;
    assert_eq!(status, axum::http::StatusCode::OK);
    assert_eq!(body["status"], json!("ok"));
}

#[tokio::test]
async fn full_workflow_build_apply_restrict_equiv_and_gc() {
    let router = test_router();

    // Create manager.
    let (st, body) = post(&router, "/managers", json!({"order": ["a", "b", "c"]})).await;
    assert_eq!(st, axum::http::StatusCode::CREATED);
    let manager_id = body["manager_id"].as_u64().unwrap();
    assert!(body["diag"]["request_id"]
        .as_str()
        .unwrap()
        .starts_with("req-"));

    // Build (a && b).
    let (st, fab) = post(
        &router,
        &format!("/managers/{manager_id}/build"),
        json!({"expr": "a && b"}),
    )
    .await;
    assert_eq!(st, axum::http::StatusCode::OK);
    let ab = node_ref(&fab);

    // Build c.
    let (_, fc) = post(
        &router,
        &format!("/managers/{manager_id}/build"),
        json!({"expr": "c"}),
    )
    .await;
    let c = node_ref(&fc);

    // OR them -> (a&&b)||c.
    let (st, forr) = post(
        &router,
        &format!("/managers/{manager_id}/apply"),
        json!({"op": "or", "lhs": ab, "rhs": c}),
    )
    .await;
    assert_eq!(st, axum::http::StatusCode::OK);
    let combined = node_ref(&forr);

    // Restrict c=false must yield a&&b.
    let (st, frest) = post(
        &router,
        &format!("/managers/{manager_id}/restrict"),
        json!({"ref": combined, "var": "c", "value": false}),
    )
    .await;
    assert_eq!(st, axum::http::StatusCode::OK);
    let restricted = node_ref(&frest);
    assert_eq!(restricted, ab, "((a&&b)||c)|c=0 must be exactly a&&b");

    // GC preserving combined; the returned root is repacked.
    let (st, fgc) = post(
        &router,
        &format!("/managers/{manager_id}/gc"),
        json!({"roots": [combined]}),
    )
    .await;
    assert_eq!(st, axum::http::StatusCode::OK);
    assert_eq!(fgc["roots"][0]["epoch"], fgc["epoch_after"]);

    // Equivalence endpoint accepts a->b vs !a||b.
    let (st, equiv) = post(
        &router,
        "/equiv",
        json!({
            "lhs": {"expr": "a -> b", "order": ["a", "b"]},
            "rhs": {"expr": "!a || b", "order": ["a", "b"]},
            "client_label": "super-secret-label"
        }),
    )
    .await;
    assert_eq!(st, axum::http::StatusCode::OK);
    assert_eq!(equiv["decision"], json!("accepted"));
    assert_eq!(equiv["equivalent"], json!(true));
    assert_eq!(equiv["oracle_checked"], json!(true));
    assert_eq!(equiv["assignments_checked"], json!(4));
    assert!(equiv["witness"].is_null());
    // The sensitive label must only appear as a redaction marker.
    let serialized = equiv.to_string();
    assert!(!serialized.contains("super-secret-label"));
    assert!(equiv["diag"]["sensitive"]["client_label"]
        .as_str()
        .unwrap()
        .starts_with("redacted("));
}

#[tokio::test]
async fn non_equivalence_is_422_with_a_distinguishing_witness() {
    let router = test_router();
    let (st, body) = post(
        &router,
        "/equiv",
        json!({
            "lhs": {"expr": "a && b", "order": ["a", "b"]},
            "rhs": {"expr": "a || b", "order": ["a", "b"]}
        }),
    )
    .await;
    assert_eq!(st, axum::http::StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["decision"], json!("rejected"));
    assert_eq!(body["equivalent"], json!(false));
    let w = &body["witness"];
    // AND vs OR differ when exactly one input is true.
    assert_ne!(w["a"], w["b"]);
    assert_eq!(body["diag"]["outcome"], json!("rejected"));
}

#[tokio::test]
async fn parse_errors_are_400_with_invalid_expr_category() {
    let router = test_router();
    let (_st, body) = post(&router, "/managers", json!({"order": ["a"]})).await;
    let id = body["manager_id"].as_u64().unwrap();
    let (st, body) = post(
        &router,
        &format!("/managers/{id}/build"),
        json!({"expr": "a && "}),
    )
    .await;
    assert_eq!(st, axum::http::StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["kind"], json!("invalid-expr"));
    assert!(body["diag"]["request_id"]
        .as_str()
        .unwrap()
        .starts_with("req-"));
}

#[tokio::test]
async fn unknown_variable_is_a_typed_422() {
    let router = test_router();
    let (_, body) = post(&router, "/managers", json!({"order": ["a"]})).await;
    let id = body["manager_id"].as_u64().unwrap();
    let (st, body) = post(
        &router,
        &format!("/managers/{id}/build"),
        json!({"expr": "a && z"}),
    )
    .await;
    assert_eq!(st, axum::http::StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["kind"], json!("unknown-variable"));
}

#[tokio::test]
async fn unknown_manager_is_404() {
    let router = test_router();
    let (st, body) = post(&router, "/managers/9999/build", json!({"expr": "a"})).await;
    assert_eq!(st, axum::http::StatusCode::NOT_FOUND);
    assert_eq!(body["error"]["kind"], json!("unknown-manager"));
}

#[tokio::test]
async fn cross_manager_reference_is_rejected() {
    let router = test_router();
    let (_, m1) = post(&router, "/managers", json!({"order": ["a"]})).await;
    let (_, m2) = post(&router, "/managers", json!({"order": ["a"]})).await;
    let id1 = m1["manager_id"].as_u64().unwrap();
    let id2 = m2["manager_id"].as_u64().unwrap();
    let (_, built) = post(
        &router,
        &format!("/managers/{id1}/build"),
        json!({"expr": "a"}),
    )
    .await;
    let r = node_ref(&built);

    let (st, body) = post(&router, &format!("/managers/{id2}/not"), json!({"ref": r})).await;
    assert_eq!(st, axum::http::StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["kind"], json!("foreign-manager"));
}

#[tokio::test]
async fn stale_reference_after_gc_is_rejected() {
    let router = test_router();
    let (_, m) = post(&router, "/managers", json!({"order": ["a", "b"]})).await;
    let id = m["manager_id"].as_u64().unwrap();
    let (_, keep_b) = post(
        &router,
        &format!("/managers/{id}/build"),
        json!({"expr": "a"}),
    )
    .await;
    let (_, drop_b) = post(
        &router,
        &format!("/managers/{id}/build"),
        json!({"expr": "b"}),
    )
    .await;
    let keep = node_ref(&keep_b);
    let throwaway = node_ref(&drop_b);

    let (st, gc) = post(
        &router,
        &format!("/managers/{id}/gc"),
        json!({"roots": [keep]}),
    )
    .await;
    assert_eq!(st, axum::http::StatusCode::OK);
    assert!(gc["collected"].as_u64().unwrap() >= 1);

    let (st, body) = post(
        &router,
        &format!("/managers/{id}/not"),
        json!({"ref": throwaway}),
    )
    .await;
    assert_eq!(st, axum::http::StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["kind"], json!("stale-reference"));
}

#[tokio::test]
async fn equivalence_with_renaming_is_accepted() {
    let router = test_router();
    let (st, body) = post(
        &router,
        "/equiv",
        json!({
            "lhs": {"expr": "x && y || !x", "order": ["x", "y"]},
            "rhs": {"expr": "p && q || !p", "order": ["p", "q"]},
            "mapping": {"x": "p", "y": "q"}
        }),
    )
    .await;
    assert_eq!(st, axum::http::StatusCode::OK);
    assert_eq!(body["decision"], json!("accepted"));
}

#[tokio::test]
async fn order_reversing_mapping_is_refused_with_category() {
    let router = test_router();
    let (st, body) = post(
        &router,
        "/equiv",
        json!({
            "lhs": {"expr": "x && y", "order": ["x", "y"]},
            "rhs": {"expr": "q && p", "order": ["q", "p"]},
            "mapping": {"x": "p", "y": "q"}
        }),
    )
    .await;
    assert_eq!(st, axum::http::StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["kind"], json!("order-mismatch"));
}

#[tokio::test]
async fn over_limit_truth_table_is_inconclusive_but_not_false() {
    let router = test_router();
    let vars: Vec<String> = (0..18).map(|i| format!("v{i}")).collect();
    let expr = vars.join(" || ");
    let (st, body) = post(
        &router,
        "/equiv",
        json!({
            "lhs": {"expr": expr, "order": vars},
            "rhs": {"expr": expr, "order": vars},
            "max_assignments": 1024
        }),
    )
    .await;
    assert_eq!(st, axum::http::StatusCode::OK);
    assert_eq!(body["decision"], json!("inconclusive"));
    assert_eq!(body["oracle_checked"], json!(false));
}

#[tokio::test]
async fn sat_endpoint_reports_witness_and_unsat() {
    let router = test_router();
    let (_, m) = post(&router, "/managers", json!({"order": ["a", "b"]})).await;
    let id = m["manager_id"].as_u64().unwrap();
    let (_, f) = post(
        &router,
        &format!("/managers/{id}/build"),
        json!({"expr": "a && b"}),
    )
    .await;
    let r = node_ref(&f);

    let (st, sat) = post(&router, &format!("/managers/{id}/sat"), json!({"ref": r})).await;
    assert_eq!(st, axum::http::StatusCode::OK);
    assert_eq!(sat["satisfiable"], json!(true));
    assert_eq!(sat["witness"]["a"], json!(true));
    assert_eq!(sat["witness"]["b"], json!(true));

    let (_, nf) = post(
        &router,
        &format!("/managers/{id}/build"),
        json!({"expr": "a && !a"}),
    )
    .await;
    let nr = node_ref(&nf);
    let (_, unsat) = post(&router, &format!("/managers/{id}/sat"), json!({"ref": nr})).await;
    assert_eq!(unsat["satisfiable"], json!(false));
    assert!(unsat["witness"].is_null());
}
