//! HTTP interface tests driven through axum's in-process router
//! (tower oneshot): concrete decisions, error categories and request ids.

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use mph_service::api::{router, AppState};
use mph_service::config::ServiceConfig;
use mph_service::kernel::builder::attempt_seed;
use mph_service::kernel::graph::{build_edges, peel};
use mph_service::kernel::builder::vertex_count;
use tower::ServiceExt;

fn test_state() -> Arc<AppState> {
    let dir = std::env::temp_dir().join(format!(
        "mph-test-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let cfg = ServiceConfig {
        listen: "127.0.0.1:0".into(),
        index_path: dir.join("index.mph").to_str().unwrap().to_string(),
        default_seed: 1592079361,
        default_max_attempts: 128,
        max_keys: 100_000,
    };
    Arc::new(AppState {
        index: std::sync::RwLock::new(None),
        cfg,
        req_counter: std::sync::atomic::AtomicU64::new(0),
    })
}

async fn get(state: Arc<AppState>, uri: &str) -> (StatusCode, serde_json::Value, String) {
    call(state, Request::builder().uri(uri).body(Body::empty()).unwrap()).await
}

async fn post(
    state: Arc<AppState>,
    uri: &str,
    json: &str,
) -> (StatusCode, serde_json::Value, String) {
    let req = Request::builder()
        .method("POST")
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(json.to_string()))
        .unwrap();
    call(state, req).await
}

async fn query_key(state: Arc<AppState>, key: &str) -> (StatusCode, serde_json::Value, String) {
    let body = serde_json::json!({ "key": key }).to_string();
    post(state, "/v1/query", &body).await
}

async fn call(
    state: Arc<AppState>,
    req: Request<Body>,
) -> (StatusCode, serde_json::Value, String) {
    let resp = router(state).oneshot(req).await.unwrap();
    let req_id = resp
        .headers()
        .get("x-request-id")
        .map(|v| v.to_str().unwrap().to_string())
        .unwrap_or_default();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let json: serde_json::Value = serde_json::from_slice(&bytes).unwrap_or_else(|_| {
        panic!("non-JSON response: {}", String::from_utf8_lossy(&bytes))
    });
    (status, json, req_id)
}

#[tokio::test]
async fn full_lifecycle_member_and_non_member() {
    let st = test_state();
    let keys = serde_json::json!(["red", "green", "blue", "yellow"])
        .to_string();
    let (status, body, _) = post(
        st.clone(),
        "/v1/index/build",
        &format!("{{\"keys\":{keys}}}"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "ok");
    assert_eq!(body["n"], 4);
    assert_eq!(body["m"], 8); // small-set rule: 2n
    assert!(body["attempts"].as_u64().unwrap() >= 1);
    assert!(body["attempts"].as_u64().unwrap() <= 128);
    assert_eq!(body["duplicates_removed"], 0);
    assert!(body["persisted_to"].as_str().unwrap().ends_with("index.mph"));

    let (status, member, _) = query_key(st.clone(), "green").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(member["decision"], "member");
    let slot = member["slot"].as_u64().unwrap();
    assert!(slot < 4);
    assert_eq!(
        member["reason"],
        "fingerprint verified at candidate slot"
    );

    let (status, nonmember, rid_hdr) = query_key(st.clone(), "purple").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(nonmember["decision"], "not_member");
    assert!(nonmember["slot"].is_null());
    let reason = nonmember["reason"].as_str().unwrap();
    assert!(
        reason.starts_with("fingerprint mismatch at candidate slot"),
        "{reason}"
    );
    assert_eq!(nonmember["request_id"], rid_hdr);
    // Masked identity: no raw key, fixed shape.
    let key_id = nonmember["key_id"].as_str().unwrap();
    assert!(key_id.starts_with("fp12="));
    assert!(key_id.contains("len=6"));

    let (status, stats, _) = get(st.clone(), "/v1/stats").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(stats["index_loaded"], true);
    assert_eq!(stats["n"], 4);
    assert_eq!(stats["format_version"], 1);
    assert_eq!(stats["algorithm_version"], 1);
}

#[tokio::test]
async fn query_before_build_is_undecidable_not_not_member() {
    let st = test_state();
    let (status, body, _) = query_key(st.clone(), "anything").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["decision"], "undecidable");
    assert_eq!(
        body["reason"],
        "no index loaded; cannot determine membership"
    );
    assert!(body["index_seed"].is_null());
}

#[tokio::test]
async fn duplicate_keys_reported_and_still_collision_free() {
    let st = test_state();
    let (status, body, _) = post(
        st.clone(),
        "/v1/index/build",
        r#"{"keys":["a","b","a","c","b"]}"#,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["n"], 3);
    assert_eq!(body["duplicates_removed"], 2);

    let mut got = Vec::new();
    for k in ["a", "b", "c"] {
        let (_, v, _) = query_key(st.clone(), k).await;
        assert_eq!(v["decision"], "member");
        got.push(v["slot"].as_u64().unwrap());
    }
    got.sort_unstable();
    assert_eq!(got, vec![0, 1, 2]);
}

#[tokio::test]
async fn empty_set_builds_and_queries_reject() {
    let st = test_state();
    let (status, body, _) = post(st.clone(), "/v1/index/build", r#"{"keys":[]}"#).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["n"], 0);
    assert_eq!(body["m"], 0);
    let (_, q, _) = query_key(st.clone(), "x").await;
    assert_eq!(q["decision"], "not_member");
    assert_eq!(q["reason"], "index is empty; no key can be a member");
}

#[tokio::test]
async fn peeling_exhaustion_returns_category_and_422() {
    let st = test_state();
    // Scan for a base seed where the first attempt (at the builder's
    // real m) leaves a 2-core and the next attempt succeeds.
    let keys = [b"aa".to_vec(), b"bb".to_vec(), b"cc".to_vec(), b"dd".to_vec()];
    let n = keys.len();
    let m = vertex_count(n);
    let mut failing_base = None;
    'outer: for base in 0..1_000_000u64 {
        let fail0 = match build_edges(attempt_seed(base, 0), m, &keys) {
            Ok(edges) => peel(&edges, m).is_err(),
            Err(_) => continue,
        };
        let ok1 = matches!(
            build_edges(attempt_seed(base, 1), m, &keys),
            Ok(edges) if peel(&edges, m).is_ok()
        );
        if fail0 && ok1 {
            failing_base = Some(base);
            break 'outer;
        }
    }
    let base = failing_base.expect("a 2-core failing seed exists");
    let payload = format!(
        r#"{{"keys":["aa","bb","cc","dd"],"seed":{base},"max_attempts":1}}"#
    );
    let (status, body, rid) = post(st.clone(), "/v1/index/build", &payload).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["category"], "peeling_exhausted");
    assert!(
        body["message"]
            .as_str()
            .unwrap()
            .contains("peeling failed for all 1"),
        "{}",
        body["message"]
    );
    assert!(body["request_id"].as_str().unwrap().starts_with("req-"));
    assert_eq!(body["request_id"], rid);
}

#[tokio::test]
async fn persisted_index_reloads_and_answers_identically() {
    let st = test_state();
    let (_, body, _) = post(
        st.clone(),
        "/v1/index/build",
        r#"{"keys":["apple","banana","cherry","date","elderberry"]}"#,
    )
    .await;
    let seed = body["seed"].as_u64().unwrap();
    let path = st.cfg.index_path.clone();

    let (status, loaded, _) = post(
        st.clone(),
        "/v1/index/load",
        &format!(r#"{{"path":"{}"}}"#, path.replace('\\', "/")),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{loaded}");
    assert_eq!(loaded["seed"], seed);
    assert_eq!(loaded["n"], 5);

    let (_, yes, _) = query_key(st.clone(), "banana").await;
    assert_eq!(yes["decision"], "member");
    assert_eq!(yes["index_seed"], seed);
    let (_, no, _) = query_key(st.clone(), "fig").await;
    assert_eq!(no["decision"], "not_member");
}

#[tokio::test]
async fn json_query_is_binary_and_unicode_safe() {
    let st = test_state();
    let weird = "a b\tc\n中文😀\x01";
    let payload = serde_json::json!({ "keys": [weird, "normal"] }).to_string();
    let (status, body, _) = post(st.clone(), "/v1/index/build", &payload).await;
    assert_eq!(status, StatusCode::OK, "{body}");

    let q = serde_json::json!({ "key": weird }).to_string();
    let (status, member, _) = post(st.clone(), "/v1/query", &q).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(member["decision"], "member");
    let id = member["key_id"].as_str().unwrap();
    assert!(id.starts_with("fp12="));
    assert!(!id.contains("中文") && !id.contains(' '));

    let q = serde_json::json!({ "key": "a b\tc\n中文😀\x02" }).to_string();
    let (_, notmember, _) = post(st.clone(), "/v1/query", &q).await;
    assert_eq!(notmember["decision"], "not_member");
}

#[tokio::test]
async fn bad_json_is_a_named_category() {
    let st = test_state();
    let (status, body, _) = post(st.clone(), "/v1/index/build", "{not json").await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["category"], "bad_request");
}

#[tokio::test]
async fn request_id_header_is_honoured_and_echoed() {
    let st = test_state();
    let req = Request::builder()
        .uri("/v1/query?key=z")
        .header("x-request-id", "trace-abc-123")
        .body(Body::empty())
        .unwrap();
    let resp = router(st).oneshot(req).await.unwrap();
    assert_eq!(
        resp.headers().get("x-request-id").unwrap(),
        "trace-abc-123"
    );
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["request_id"], "trace-abc-123");
}
