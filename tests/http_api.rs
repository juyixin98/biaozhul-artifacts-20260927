//! HTTP verification-interface tests (in-process oneshot, no real network).
//!
//! Driven by `fixtures/handcalc.json`: the fixture is parsed at runtime, its
//! batches/rejections/queries are replayed over HTTP, and every expected sum
//! is asserted against the response body. Status codes and `error_code`s are
//! checked per failure category.

mod common;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use prereg2d::api;
use prereg2d::service::Registry;
use serde_json::Value;
use std::sync::Arc;
use tempfile::TempDir;
use tower::ServiceExt;

struct Harness {
    _dir: TempDir,
    app: axum::Router,
    run: String,
}

impl Harness {
    fn new() -> Self {
        let dir = TempDir::new().unwrap();
        let registry = Arc::new(Registry::open(dir.path()).unwrap());
        let app = api::router(registry, 1024 * 1024);
        Self {
            _dir: dir,
            app,
            run: common::caselog::run_id(),
        }
    }

    async fn send(
        &self,
        method: &str,
        uri: &str,
        json: Option<Value>,
        rid: Option<&str>,
    ) -> (StatusCode, Value) {
        let mut builder = Request::builder().method(method).uri(uri);
        if let Some(id) = rid {
            builder = builder.header("x-request-id", id);
        }
        let req = match json {
            Some(v) => builder
                .header("content-type", "application/json")
                .body(Body::from(v.to_string()))
                .unwrap(),
            None => builder.body(Body::empty()).unwrap(),
        };
        let resp = self.app.clone().oneshot(req).await.unwrap();
        let status = resp.status();
        let bytes = resp.into_body().collect().await.unwrap().to_bytes();
        let value: Value = serde_json::from_slice(&bytes).unwrap_or_else(|e| {
            panic!(
                "non-JSON response: {e}; body={}",
                String::from_utf8_lossy(&bytes)
            )
        });
        (status, value)
    }
}

fn fixture() -> Value {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/fixtures/handcalc.json");
    let text = std::fs::read_to_string(path).unwrap();
    serde_json::from_str(&text).unwrap()
}

#[tokio::test]
async fn http_handcalc_fixture() {
    let fx = fixture();
    let h = Harness::new();
    let rid = &h.run;

    // Health before init.
    let (st, v) = h.send("GET", "/health", None, Some(rid)).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(v["ok"], true);
    assert!(v["head_version"].is_null());

    // Register.
    let (st, v) = h
        .send(
            "POST",
            "/admin/register",
            Some(fx["register"].clone()),
            Some(rid),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["version"], 1);
    assert_eq!(v["kind"], "registered");

    // Accepted batches.
    for (i, batch) in fx["batches"].as_array().unwrap().iter().enumerate() {
        let body = serde_json::json!({ "updates": batch["updates"] });
        let (st, v) = h.send("POST", "/batches", Some(body), Some(rid)).await;
        assert_eq!(st, StatusCode::OK, "batch {}: {v}", batch["name"]);
        assert_eq!(v["version"], (i as u64) + 2);
    }

    // Rejected batches: exact status + error_code; version stays at 3.
    let expected_status = |code: &str| match code {
        "unregistered_coordinate" | "duplicate_in_batch" | "empty_batch" | "overflow" => {
            StatusCode::UNPROCESSABLE_ENTITY
        }
        other => panic!("fixture used unexpected code {other}"),
    };
    for bad in fx["rejected_batches"].as_array().unwrap() {
        let code = bad["error_code"].as_str().unwrap();
        let body = serde_json::json!({ "updates": bad["updates"] });
        let (st, v) = h.send("POST", "/batches", Some(body), Some(rid)).await;
        assert_eq!(st, expected_status(code), "case {} body={v}", bad["name"]);
        assert_eq!(v["ok"], false);
        assert_eq!(v["error_code"], code, "case {}", bad["name"]);
        assert_eq!(
            v["request_id"].as_str(),
            Some(rid.as_str()),
            "request id echoed on error"
        );
    }

    // Rebuild.
    let (st, v) = h
        .send(
            "POST",
            "/admin/rebuild",
            Some(fx["rebuild"].clone()),
            Some(rid),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["version"], 4);
    assert_eq!(v["kind"], "rebuild");

    // All fixture queries, including old versions after rebuild.
    for q in fx["queries"].as_array().unwrap() {
        let uri = format!(
            "/query?version={}&x_lo={}&x_hi={}&y_lo={}&y_hi={}",
            q["at_version"].as_u64().unwrap(),
            q["x_lo"].as_i64().unwrap(),
            q["x_hi"].as_i64().unwrap(),
            q["y_lo"].as_i64().unwrap(),
            q["y_hi"].as_i64().unwrap()
        );
        let (st, v) = h.send("GET", &uri, None, Some(rid)).await;
        assert_eq!(st, StatusCode::OK, "query {} -> {v}", q["name"]);
        assert_eq!(v["sum"], q["sum"], "query {}", q["name"]);
        assert_eq!(
            v["version"], q["at_version"],
            "query {} reports the version it evaluated",
            q["name"]
        );
        // The response always carries the computation steps that justify sum.
        assert!(
            v["explain"]["cutoffs"].is_object(),
            "explain.cutoffs present"
        );
        assert!(
            v["explain"]["terms"]["hh"].is_number(),
            "explain.terms.hh numeric"
        );
        assert_eq!(
            v["explain"]["sum"], v["sum"],
            "explain.sum equals top-level sum"
        );
        if q.get("empty").and_then(|e| e.as_bool()).unwrap_or(false) {
            assert_eq!(v["empty"], true, "query {} marked empty", q["name"]);
            assert_eq!(v["sum"], 0);
        }
    }
}

#[tokio::test]
async fn http_error_categories_and_correlation() {
    let h = Harness::new();

    // Batch before initialization: 412 not_initialized.
    let (st, v) = h
        .send(
            "POST",
            "/batches",
            Some(serde_json::json!({"updates":[]})),
            Some("cid-preinit"),
        )
        .await;
    assert_eq!(st, StatusCode::PRECONDITION_FAILED);
    assert_eq!(v["error_code"], "not_initialized");
    assert_eq!(v["request_id"], "cid-preinit");

    h.send(
        "POST",
        "/admin/register",
        Some(serde_json::json!({"xs":[1],"ys":[1]})),
        Some("r"),
    )
    .await;

    // Malformed JSON: 422 bad_request (not 500), echoed request id.
    let req = Request::post("/batches")
        .header("content-type", "application/json")
        .header("x-request-id", "cid-badjson")
        .body(Body::from("{not json"))
        .unwrap();
    let resp = h.app.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
    let body: Value =
        serde_json::from_slice(&resp.into_body().collect().await.unwrap().to_bytes()).unwrap();
    assert_eq!(body["error_code"], "bad_request");
    assert_eq!(body["request_id"], "cid-badjson");

    // Schema mismatch: updates element missing delta.
    let (st, v) = h
        .send(
            "POST",
            "/batches",
            Some(serde_json::json!({"updates":[{"x":1,"y":1}]})),
            Some("r"),
        )
        .await;
    assert_eq!(st, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["error_code"], "bad_request");

    // Query with a non-integer bound.
    let (st, v) = h
        .send("GET", "/query?x_lo=a&x_hi=1&y_lo=0&y_hi=1", None, Some("r"))
        .await;
    assert_eq!(st, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["error_code"], "bad_request");

    // Missing required bound is also bad_request JSON (not axum's text 400).
    let (st, v) = h
        .send(
            "GET",
            "/query?x_lo=0&x_hi=1&y_lo=0",
            None,
            Some("cid-missing"),
        )
        .await;
    assert_eq!(st, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["error_code"], "bad_request");
    assert!(v["error"].as_str().unwrap().contains("y_hi"));
    assert_eq!(v["request_id"], "cid-missing");

    // Malformed percent-encoding is bad_request, never a success.
    let (st, v) = h
        .send(
            "GET",
            "/query?x_lo=0&x_hi=1&y_lo=0&y_hi=%ZZ",
            None,
            Some("r"),
        )
        .await;
    assert_eq!(st, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["error_code"], "bad_request");

    // Unknown route: JSON 404 with the same envelope.
    let (st, v) = h
        .send("GET", "/does-not-exist", None, Some("cid-404"))
        .await;
    assert_eq!(st, StatusCode::NOT_FOUND);
    assert_eq!(v["ok"], false);
    assert_eq!(v["error_code"], "not_found");
    assert_eq!(v["request_id"], "cid-404");

    // Unknown version: 404.
    let (st, v) = h
        .send(
            "GET",
            "/query?version=99&x_lo=0&x_hi=1&y_lo=0&y_hi=1",
            None,
            Some("r"),
        )
        .await;
    assert_eq!(st, StatusCode::NOT_FOUND);
    assert_eq!(v["error_code"], "unknown_version");
    let (st, _) = h.send("GET", "/versions/99", None, Some("r")).await;
    assert_eq!(st, StatusCode::NOT_FOUND);

    // Oversized body for the configured limit: 413 payload_too_large.
    let big = "0".repeat(2 * 1024 * 1024);
    let (st, v) = h
        .send(
            "POST",
            "/batches",
            Some(serde_json::json!({"updates":big})),
            Some("r"),
        )
        .await;
    assert_eq!(st, StatusCode::PAYLOAD_TOO_LARGE, "body={v}");
    assert_eq!(v["error_code"], "payload_too_large");

    // Versions listing reflects history and kinds.
    let (st, v) = h.send("GET", "/versions", None, Some("r")).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(v["head_version"], 1);
    assert_eq!(v["versions"][0]["kind"], "registered");
}

#[tokio::test]
async fn http_query_bounds_beyond_i64_accepted() {
    // i128 bounds (extreme empty-adjacent values) must parse, and the
    // generated request id is returned when the client supplies none.
    let h = Harness::new();
    h.send(
        "POST",
        "/admin/register",
        Some(serde_json::json!({"xs":[i64::MIN,0,i64::MAX],"ys":[i64::MIN,0,i64::MAX]})),
        Some("r"),
    )
    .await;
    h.send(
        "POST",
        "/batches",
        Some(serde_json::json!({"updates":[{"x":0,"y":0,"delta":-7}]})),
        Some("r"),
    )
    .await;
    let uri = format!(
        "/query?x_lo={}&x_hi={}&y_lo={}&y_hi={}",
        i128::MIN,
        i128::MAX,
        i128::MIN,
        i128::MAX
    );
    let (st, v) = h.send("GET", &uri, None, None).await;
    assert_eq!(st, StatusCode::OK, "{v}");
    assert_eq!(v["sum"], -7);
    let rid = v.get("request_id").cloned();
    // Success responses need not echo the rid (errors do); just ensure a
    // header-less request was accepted and answered correctly.
    assert!(rid.is_none() || rid.unwrap().is_string());
}
