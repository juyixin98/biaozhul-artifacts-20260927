//! HTTP backend tests using tower's oneshot service (no live socket needed).
//! These assert on status codes, request identity, verdict categories and the
//! failure/uncertainty separation — not just that endpoints answer.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use interval_analyzer::api::{router, AnalyzeRequest, VerifyRequest};
use interval_analyzer::config::Config;
use interval_analyzer::report::Verdict;
use serde_json::{json, Value};
use tower::ServiceExt;

fn app() -> axum::Router {
    router(interval_analyzer::api::AppState {
        default_config: Config::default(),
    })
}

async fn body_json(resp: axum::response::Response) -> Value {
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    match serde_json::from_slice::<Value>(&bytes) {
        Ok(v) => v,
        Err(e) => panic!(
            "non-JSON body ({status}): {e}: {}",
            String::from_utf8_lossy(&bytes)
        ),
    }
}

async fn post(uri: &str, payload: Value) -> (StatusCode, Value) {
    let resp = app()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(uri)
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(&payload).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    (status, body_json(resp).await)
}

#[tokio::test]
async fn health_and_version() {
    let resp = app()
        .oneshot(
            Request::builder()
                .uri("/health")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let j = body_json(resp).await;
    assert_eq!(j["status"], "ok");
    assert!(j["analyzer_version"].is_string());
}

#[tokio::test]
async fn analyze_returns_identity_version_positions_and_separated_categories() {
    let src = "let i: [0, 20];\narray a[10];\nx := a[i];\nj := 0 - 5;\ny := a[j];\n";
    let (status, j) = post("/v1/analyze", json!({ "source": src })).await;
    assert_eq!(status, StatusCode::OK);
    // request identity + version
    let rid = j["request_id"].as_str().unwrap();
    assert!(rid.starts_with("req-"), "{rid}");
    assert!(j["analyzer_version"].is_string());
    assert_eq!(j["analyzer_version"], j["report"]["analyzer_version"]);

    let report = &j["report"];
    // summary separates definite from possible
    let summary = &report["summary"];
    assert_eq!(summary["possible_violations"], 1);
    assert_eq!(summary["definite_violations"], 1);
    let maybe_id = summary["possible_violation_ids"][0].as_u64().unwrap() as usize;
    let def_id = summary["definite_violation_ids"][0].as_u64().unwrap() as usize;
    assert_ne!(maybe_id, def_id);

    let checks = report["checks"].as_array().unwrap();
    let by_id: std::collections::HashMap<_, _> = checks
        .iter()
        .map(|c| (c["id"].as_u64().unwrap() as usize, c))
        .collect();
    assert_eq!(by_id[&maybe_id]["verdict"], "maybe_violated");
    assert_eq!(by_id[&maybe_id]["kind"], "array_index");
    assert_eq!(by_id[&maybe_id]["span"]["line"], 3);
    assert_eq!(by_id[&def_id]["verdict"], "violated");
    assert_eq!(by_id[&def_id]["span"]["line"], 5);
    // structured evidence present
    assert_eq!(by_id[&maybe_id]["evidence"]["kind"], "array_index");
    assert!(by_id[&maybe_id]["evidence"]["index"].is_object());
}

#[tokio::test]
async fn parse_error_is_400_with_position() {
    let (status, j) = post("/v1/analyze", json!({ "source": "let x: [;" })).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(j["request_id"].is_string());
    assert_eq!(j["error"]["kind"], "parse_error");
    assert!(j["error"]["line"].is_u64());
    assert!(j["error"]["col"].is_u64());
}

#[tokio::test]
async fn semantic_error_is_400_for_undeclared_variable() {
    let (status, j) = post("/v1/analyze", json!({ "source": "y := x + 1;" })).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(j["error"]["kind"], "semantic_error");
    assert!(j["error"]["message"]
        .as_str()
        .unwrap()
        .contains("before declaration"));
}

#[tokio::test]
async fn verify_roundtrip_accepts_and_rejects_tampered_report() {
    let src = "let n: [0, 4];\ni := 0;\nwhile i < n {\n  i := i + 1;\n}\n";
    let (_, analyzed) = post("/v1/analyze", json!({ "source": src })).await;
    let report = &analyzed["report"];

    let req = json!({ "source": src, "report": report });
    let (status, v) = post("/v1/verify", req).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["verification"]["ok"], true);

    // tamper: flip first check verdict
    let mut tampered: Value = report.clone();
    let first = tampered["checks"]
        .as_array_mut()
        .unwrap()
        .get_mut(0)
        .cloned();
    if let Some(mut c) = first {
        c["verdict"] = json!("violated");
        let arr = tampered["checks"].as_array_mut().unwrap();
        arr[0] = c;
        let (status2, v2) = post("/v1/verify", json!({ "source": src, "report": tampered })).await;
        assert_eq!(status2, StatusCode::OK);
        assert_eq!(v2["verification"]["ok"], false);
        assert!(v2["verification"]["failures"]
            .as_array()
            .unwrap()
            .iter()
            .any(|f| f["location"].as_str().unwrap().contains("check[")));
    }
}

#[tokio::test]
async fn config_override_changes_fixpoint_behavior() {
    // A loop with narrowing disabled keeps the widened scalar at +inf; with
    // narrowing enabled it pulls back. The response echoes the config used.
    let src = "let n: [0, 10];\ni := 0;\nwhile i < n {\n  i := i + 1;\n}\n";
    let (_, wide) = post(
        "/v1/analyze",
        json!({ "source": src, "config": { "enable_narrowing": false } }),
    )
    .await;
    let i_wide = &wide["report"]["loop_invariants"][0]["invariant"]["vars"]["i"];
    assert_eq!(
        i_wide["R"]["hi"], "PosInf",
        "without narrowing i should stay at +inf: {i_wide}"
    );
    assert_eq!(wide["report"]["config"]["enable_narrowing"], false);

    let (_, narrow) = post("/v1/analyze", json!({ "source": src })).await;
    let i_narrow = &narrow["report"]["loop_invariants"][0]["invariant"]["vars"]["i"];
    assert_eq!(
        i_narrow["R"]["hi"]["Fin"], "10",
        "with narrowing i should be capped at 10: {i_narrow}"
    );
}

#[test]
fn request_types_deserialize() {
    // compile-time shape guard for the public request contract
    let _: AnalyzeRequest = serde_json::from_value(json!({"source": "let x: [0,1];"})).unwrap();
    let bad: Result<VerifyRequest, _> = serde_json::from_value(json!({"source": "", "report": {}}));
    assert!(bad.is_err());
    let _ = Verdict::Safe;
}
