//! HTTP 端到端集成测试：真实 TCP 启动 Axum，断言**具体状态码与 error_code**，
//! 而不只是“接口能调用”。覆盖插入/查询/删除凭证流、攻击拒绝、容量耗尽状态码、
//! 请求 ID 关联、base64url 键。

use std::time::Duration;

use cuckoo_api::app::router;
use cuckoo_api::server::test_state;
use cuckoo_core::FilterParams;
use tempfile::TempDir;

use hyper::body::Bytes;
use hyper::Request;
use hyper_util::rt::TokioIo;
use serde_json::Value;
use tokio::net::TcpStream;
use tower::ServiceExt;

// 直接对 Router 发内存请求（无需端口），简单可靠。
async fn call(
    app: axum::Router,
    method: &str,
    uri: &str,
    body: Value,
    req_id: Option<&str>,
) -> (u16, Value, Option<String>) {
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(rid) = req_id {
        builder = builder.header("x-request-id", rid);
    }
    let req = builder
        .header("content-type", "application/json")
        .body(axum::body::Body::from(body.to_string()))
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    let status = resp.status().as_u16();
    let header_rid = resp
        .headers()
        .get("x-request-id")
        .map(|v| v.to_str().unwrap().to_string());
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let v: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, v, header_rid)
}

fn app() -> (axum::Router, TempDir) {
    let dir = TempDir::new().unwrap();
    let p = FilterParams::new(8, 4, 12, 200).unwrap();
    let state = test_state(dir.path(), p).unwrap();
    (router(state), dir)
}

#[tokio::test]
async fn health_reports_version_and_ok() {
    let (app, _d) = app();
    let (status, v, _) = call(app, "GET", "/health", Value::Null, None).await;
    assert_eq!(status, 200);
    assert_eq!(v["ok"], true);
    assert_eq!(v["status"], "serving");
    assert!(v["version"]["core_version"].is_string());
    assert_eq!(v["version"]["format_version"], 1);
}

#[tokio::test]
async fn insert_lookup_delete_happy_path() {
    let (app, _d) = app();
    // 插入前查不到。
    let (s, v, _) = call(
        app.clone(),
        "POST",
        "/v1/filter/lookup",
        serde_json::json!({"key": "hello"}),
        None,
    )
    .await;
    assert_eq!(s, 200);
    assert_eq!(v["member"], false);

    // 插入。
    let (s, v, _) = call(
        app.clone(),
        "POST",
        "/v1/filter/insert",
        serde_json::json!({"key": "hello"}),
        Some("rid-insert-1"),
    )
    .await;
    assert_eq!(s, 200, "{v}");
    assert_eq!(v["ok"], true);
    let credential = v["credential"].as_str().unwrap().to_string();
    assert!(credential.contains('.'));
    assert!(v["detail"]["kicks"].is_number());

    // 插入后可查（无假阴性）。
    let (s, v, _) = call(
        app.clone(),
        "POST",
        "/v1/filter/lookup",
        serde_json::json!({"key": "hello"}),
        None,
    )
    .await;
    assert_eq!(s, 200);
    assert_eq!(v["member"], true);

    // 无凭证删除 => 403 INVALID_CREDENTIAL。
    let (s, v, _) = call(
        app.clone(),
        "POST",
        "/v1/filter/delete",
        serde_json::json!({"key": "hello", "credential": "garbage"}),
        None,
    )
    .await;
    assert_eq!(s, 403);
    assert_eq!(v["ok"], false);
    assert_eq!(v["error_code"], "INVALID_CREDENTIAL");

    // 持凭证删除 => 200。
    let (s, v, echo) = call(
        app.clone(),
        "POST",
        "/v1/filter/delete",
        serde_json::json!({"key": "hello", "credential": credential}),
        Some("rid-delete-1"),
    )
    .await;
    assert_eq!(s, 200, "{v}");
    assert_eq!(v["removed"], true);
    // 请求 ID 被原样回传（可关联）。
    assert_eq!(echo.as_deref(), Some("rid-delete-1"));

    // 重放 => 409 CREDENTIAL_EXHAUSTED。
    let (s, v, _) = call(
        app.clone(),
        "POST",
        "/v1/filter/delete",
        serde_json::json!({"key": "hello", "credential": credential}),
        None,
    )
    .await;
    assert_eq!(s, 409);
    assert_eq!(v["error_code"], "CREDENTIAL_EXHAUSTED");
}

#[tokio::test]
async fn credential_bound_to_key_is_forbidden_for_other_key() {
    let (app, _d) = app();
    let (_, ins, _) = call(
        app.clone(),
        "POST",
        "/v1/filter/insert",
        serde_json::json!({"key": "alice"}),
        None,
    )
    .await;
    let cred = ins["credential"].as_str().unwrap();
    let (s, v, _) = call(
        app.clone(),
        "POST",
        "/v1/filter/delete",
        serde_json::json!({"key": "bob", "credential": cred}),
        None,
    )
    .await;
    assert_eq!(s, 403);
    assert_eq!(v["error_code"], "INVALID_CREDENTIAL");
}

#[tokio::test]
async fn bad_requests_are_400_not_success() {
    let (app, _d) = app();
    // 空键。
    let (s, v, _) = call(
        app.clone(),
        "POST",
        "/v1/filter/insert",
        serde_json::json!({"key": ""}),
        None,
    )
    .await;
    assert_eq!(s, 400);
    assert_eq!(v["error_code"], "BAD_REQUEST");

    // 未知编码。
    let (s, v, _) = call(
        app.clone(),
        "POST",
        "/v1/filter/lookup",
        serde_json::json!({"key": "x", "key_encoding": "rot13"}),
        None,
    )
    .await;
    assert_eq!(s, 400);
    assert_eq!(v["error_code"], "BAD_REQUEST");

    // 非 JSON / 缺字段（axum 解析失败 -> 400）。
    let req = Request::builder()
        .method("POST")
        .uri("/v1/filter/insert")
        .header("content-type", "application/json")
        .body(axum::body::Body::from("{ not json"))
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status().as_u16(), 400);
}

#[tokio::test]
async fn base64url_key_roundtrip() {
    let (app, _d) = app();
    // "二进制\x00键" 的无填充 base64url。
    let raw = b"binary\x00key";
    let b64 = base64url_no_pad(raw);
    let body = serde_json::json!({"key": b64, "key_encoding": "base64url"});
    let (s, v, _) = call(app.clone(), "POST", "/v1/filter/insert", body.clone(), None).await;
    assert_eq!(s, 200, "{v}");
    let cred = v["credential"].as_str().unwrap().to_string();

    let (s, v, _) = call(
        app.clone(),
        "POST",
        "/v1/filter/lookup",
        body.clone(),
        None,
    )
    .await;
    assert_eq!(s, 200);
    assert_eq!(v["member"], true);

    let (s, _, _) = call(
        app.clone(),
        "POST",
        "/v1/filter/delete",
        serde_json::json!({"key": b64, "key_encoding": "base64url", "credential": cred}),
        None,
    )
    .await;
    assert_eq!(s, 200);
}

#[tokio::test]
async fn capacity_exhaustion_returns_507_filter_full() {
    let dir = TempDir::new().unwrap();
    // 4 桶 * 2 槽 = 8 槽。
    let p = FilterParams::new(2, 2, 4, 10).unwrap();
    let state = test_state(dir.path(), p).unwrap();
    let app = router(state);

    let mut occupied = 0;
    let mut first_full: Option<u16> = None;
    for n in 0..200u64 {
        let (s, v, _) = call(
            app.clone(),
            "POST",
            "/v1/filter/insert",
            serde_json::json!({"key": format!("k{n}")}),
            None,
        )
        .await;
        if s == 200 {
            occupied += 1;
        } else {
            assert_eq!(s, 507, "容量失败应为 507，实际 {s} body={v}");
            assert_eq!(v["error_code"], "FILTER_FULL");
            first_full.get_or_insert(s);
        }
    }
    assert!(first_full.is_some(), "小容量配置必须出现 507");
    assert!(occupied <= 8, "物理槽上限 8，实际 {occupied}");
}

#[tokio::test]
async fn request_id_is_generated_when_absent() {
    let (app, _d) = app();
    let (_, _, echo) = call(app, "GET", "/health", Value::Null, None).await;
    let rid = echo.expect("未提供 X-Request-Id 时应生成一个");
    assert!(rid.starts_with("run-"), "{rid}");
}

// 真实 TCP 冒烟：证明 oneshot 之外，hyper + axum 组合在网络栈上也工作。
#[tokio::test]
async fn real_tcp_listener_smoke() {
    let dir = TempDir::new().unwrap();
    let p = FilterParams::new(6, 4, 8, 100).unwrap();
    let state = test_state(dir.path(), p).unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, router(state)).await.unwrap();
    });
    tokio::time::sleep(Duration::from_millis(50)).await;

    let stream = TcpStream::connect(addr).await.unwrap();
    let io = TokioIo::new(stream);
    let (mut sender, conn) = hyper::client::conn::http1::handshake(io).await.unwrap();
    // hyper 1.x：连接 IO 必须独立驱动。
    tokio::spawn(async move {
        let _ = conn.await;
    });
    sender.ready().await.unwrap();
    let resp = sender
        .send_request(
            Request::builder()
                .method("GET")
                .uri("/health")
                .header("host", "localhost")
                .body(http_body_util::Empty::<Bytes>::new())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
}

fn base64url_no_pad(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::new();
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = if chunk.len() > 1 { chunk[1] as u32 } else { 0 };
        let b2 = if chunk.len() > 2 { chunk[2] as u32 } else { 0 };
        let t = (b0 << 16) | (b1 << 8) | b2;
        out.push(T[((t >> 18) & 63) as usize] as char);
        out.push(T[((t >> 12) & 63) as usize] as char);
        if chunk.len() > 1 {
            out.push(T[((t >> 6) & 63) as usize] as char);
        }
        if chunk.len() > 2 {
            out.push(T[(t & 63) as usize] as char);
        }
    }
    out
}
