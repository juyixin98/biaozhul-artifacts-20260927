//! End-to-end HTTP tests against the in-process Axum router (no real port).
//!
//! These assert concrete response bodies and failure categories: request-id
//! propagation, rank/select answers, corruption 422 categories, the
//! independent-oracle cross-check verdict, and that uncertain conclusions
//! are reported in their own field.

use std::sync::Arc;

use axum::Router;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use hbs_config::Config;
use hbs_core::HierBitmap;
use hbs_server::AppState;
use hbs_store::FileStore;
use http_body_util::BodyExt;
use tower::ServiceExt;

fn temp_dir(tag: &str) -> std::path::PathBuf {
    let p = std::env::temp_dir().join(format!(
        "hbs-api-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&p).unwrap();
    p
}

fn app(dir: &std::path::Path) -> Router {
    let store = FileStore::open(dir, false).unwrap();
    let config = Config {
        data_dir: dir.to_string_lossy().to_string(),
        max_request_bytes: 1 << 20,
        ..Config::default()
    };
    hbs_server::build_app(AppState {
        store: Arc::new(store),
        config: Arc::new(config),
    })
}

async fn send(
    app: &Router,
    method: &str,
    uri: &str,
    body: Option<&str>,
    req_id: Option<&str>,
) -> (StatusCode, String, String) {
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(id) = req_id {
        builder = builder.header("x-request-id", id);
    }
    let body = match body {
        Some(b) => {
            builder = builder.header("content-type", "application/json");
            Body::from(b.to_string())
        }
        None => Body::empty(),
    };
    let resp = app
        .clone()
        .oneshot(builder.body(body).unwrap())
        .await
        .unwrap();
    let status = resp.status();
    let returned_id = resp
        .headers()
        .get("x-request-id")
        .map(|v| v.to_str().unwrap_or("").to_string())
        .unwrap_or_default();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        String::from_utf8(bytes.to_vec()).unwrap(),
        returned_id,
    )
}

// The server returns the id only via the body (not a response header); this
// helper pulls it out of the JSON text without a JSON dependency.
fn json_field(body: &str, field: &str) -> Option<String> {
    let needle = format!("\"{field}\":");
    let start = body.find(&needle)? + needle.len();
    let rest = &body[start..];
    let rest = rest.trim_start();
    if let Some(s) = rest.strip_prefix('"') {
        let end = s.find('"')?;
        Some(s[..end].to_string())
    } else {
        let end = rest.find([',', '}']).unwrap_or(rest.len());
        Some(rest[..end].to_string())
    }
}

#[tokio::test]
async fn request_id_is_echoed_and_generated() {
    let dir = temp_dir("rid");
    let app = app(&dir);
    let (status, body, _) = send(&app, "GET", "/api/v1/health", None, Some("req-123")).await;
    assert_eq!(status, StatusCode::OK);
    assert!(body.contains("\"request_id\":\"req-123\""), "body: {body}");
    assert!(body.contains("\"steps\""));
    assert!(body.contains("\"version\":\"0.1.0\""));

    // No header -> server generates a 32-char simple UUID.
    let (_, body, _) = send(&app, "GET", "/api/v1/health", None, None).await;
    let id = json_field(&body, "request_id").unwrap();
    assert_eq!(id.len(), 32, "generated id: {id}");
    assert!(id.chars().all(|c| c.is_ascii_hexdigit()));
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn create_query_rank_select_end_to_end() {
    let dir = temp_dir("crud");
    let app = app(&dir);

    // Create with interleaved values; duplicates collapse.
    let mut values: Vec<u32> = (0..5000u32).collect();
    values.push(0);
    values.push(u32::MAX);
    let body = format!(
        "{{\"name\":\"nums\",\"values\":[{}]}}",
        values
            .iter()
            .map(|v| v.to_string())
            .collect::<Vec<_>>()
            .join(",")
    );
    let (status, body, _) = send(&app, "POST", "/api/v1/sets", Some(&body), Some("c1")).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert!(body.contains("\"bitmap_containers\":1"));

    // rank exclusive / inclusive at a known point
    let (_, body, _) = send(
        &app,
        "POST",
        "/api/v1/sets/nums/rank",
        Some("{\"value\":100,\"inclusive\":true}"),
        Some("r1"),
    )
    .await;
    assert_eq!(json_field(&body, "rank").unwrap(), "101");
    assert_eq!(json_field(&body, "request_id").unwrap(), "r1");

    let (_, body, _) = send(
        &app,
        "POST",
        "/api/v1/sets/nums/rank",
        Some("{\"value\":100}"),
        None,
    )
    .await;
    assert_eq!(json_field(&body, "rank").unwrap(), "100");

    // select boundary members
    let (_, body, _) = send(
        &app,
        "POST",
        "/api/v1/sets/nums/select",
        Some("{\"rank\":0}"),
        None,
    )
    .await;
    assert_eq!(json_field(&body, "value").unwrap(), "0");
    assert_eq!(json_field(&body, "found").unwrap(), "true");

    let (_, body, _) = send(
        &app,
        "POST",
        "/api/v1/sets/nums/select",
        Some(&format!("{{\"rank\":{}}}", 5000)),
        None,
    )
    .await;
    assert_eq!(json_field(&body, "value").unwrap(), "4294967295");

    // out-of-range select: success with found=false AND an uncertainty note
    let (status, body, _) = send(
        &app,
        "POST",
        "/api/v1/sets/nums/select",
        Some("{\"rank\":999999999}"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(json_field(&body, "found").unwrap(), "false");
    assert!(
        body.contains("\"uncertainties\":[") && !body.contains("\"uncertainties\":[]"),
        "uncertain conclusion must be listed separately: {body}"
    );

    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn not_found_and_bad_request_categories() {
    let dir = temp_dir("errs");
    let app = app(&dir);

    let (status, body, _) = send(&app, "GET", "/api/v1/sets/missing", None, None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert!(body.contains("\"error_code\":\"not_found\""), "{body}");

    let (status, body, _) = send(&app, "POST", "/api/v1/sets", Some("{not json"), None).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body.contains("\"error_code\":\"bad_request\""));

    let (status, _body, _) = send(
        &app,
        "POST",
        "/api/v1/sets",
        Some("{\"name\":\"../escape\",\"values\":[]}"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn corrupt_file_returns_specific_422_category() {
    let dir = temp_dir("corrupt-api");
    let store = FileStore::open(&dir, false).unwrap();
    let mut s = HierBitmap::new();
    for v in 0..60_000u32 {
        s.insert(v);
    }
    store.save("broken", &s).unwrap();
    let path = dir.join("broken.hbs");
    let mut bytes = std::fs::read(&path).unwrap();
    let flip = bytes.len() - 5;
    bytes[flip] ^= 0xFF;
    std::fs::write(&path, bytes).unwrap();

    let app = app(&dir);
    let (status, body, _) = send(&app, "GET", "/api/v1/sets/broken", None, Some("c-1")).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert!(
        body.contains("\"error_code\":\"corrupt_checksum\""),
        "body: {body}"
    );
    assert!(body.contains("\"request_id\":\"c-1\""));

    let (status, body, _) = send(&app, "GET", "/api/v1/sets/broken/verify", None, None).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert!(body.contains("corrupt_checksum"));

    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn cross_check_endpoint_uses_independent_oracle() {
    let dir = temp_dir("oracle");
    let app = app(&dir);
    // Two overlapping sets; the verdict must be true.
    let body = "{\"a\":[1,2,3,65536,65537],\"b\":[2,3,4,65537,65538]}";
    let (status, body, _) = send(
        &app,
        "POST",
        "/api/v1/verify/cross-check",
        Some(body),
        Some("x1"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert!(body.contains("\"passed\":true"), "{body}");
    assert!(body.contains("oracle"));
    assert!(body.contains("\"request_id\":\"x1\""));

    // Malformed: missing `a`.
    let (status, body, _) = send(
        &app,
        "POST",
        "/api/v1/verify/cross-check",
        Some("{\"b\":[]}"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body.contains("bad_request"));
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn algebra_endpoint_roundtrip_and_persist() {
    let dir = temp_dir("algebra");
    let app = app(&dir);
    let mk = |name: &str, vals: &[u32]| {
        format!(
            "{{\"name\":\"{name}\",\"values\":[{}]}}",
            vals.iter()
                .map(|v| v.to_string())
                .collect::<Vec<_>>()
                .join(",")
        )
    };
    let (status, _, _) = send(
        &app,
        "POST",
        "/api/v1/sets",
        Some(&mk("a", &[1, 2, 3])),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let (status, _, _) = send(
        &app,
        "POST",
        "/api/v1/sets",
        Some(&mk("b", &[2, 3, 4])),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);

    let (status, body, _) = send(
        &app,
        "POST",
        "/api/v1/algebra/intersection?x=1",
        Some("{\"left\":\"a\",\"right\":\"b\",\"save_as\":\"c\"}"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(body.contains("\"cardinality\":2"), "{body}");

    let (_, body, _) = send(&app, "GET", "/api/v1/sets/c", None, None).await;
    assert!(
        body.contains("\"cardinality\":2"),
        "persisted result: {body}"
    );

    // unknown op is a bad_request with an explicit category
    let (status, body, _) = send(
        &app,
        "POST",
        "/api/v1/algebra/bogus",
        Some("{\"left\":\"a\",\"right\":\"b\"}"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body.contains("\"error_code\":\"unknown_op\""));
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn fixtures_endpoint_covers_all_distributions() {
    let dir = temp_dir("fixtures");
    let app = app(&dir);
    let (status, body, _) = send(
        &app,
        "POST",
        "/api/v1/fixtures",
        Some("{\"distribution\":\"all\"}"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(body.contains("\"passed\":true"), "{body}");
    // 4 distributions x 3 seeds = 12 results
    let count = body.matches("\"label\":").count();
    assert_eq!(count, 12);
    let _ = std::fs::remove_dir_all(&dir);
}
