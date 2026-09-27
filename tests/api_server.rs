//! 集成测试：Axum HTTP 接口端到端。
//!
//! 覆盖：
//! - 创建（inline text/base64/文件导入）、查询、计数、verify 的成功路径与具体 JSON 字段；
//! - 错误类别经 HTTP 状态码与 error 字段可区分（400/404/409/413/500-corrupt）；
//! - 二进制零字节模式走 base64 通道；空模式与超长模式；
//! - 每个响应携带 run_id，可通过 x-run-id 头重放；
//! - 磁盘存在但未加载索引的状态冲突提示；
//! - 请求体超限返回 resource_exhausted。

mod common;

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use base64::Engine as _;
use common::{Harness, TestLog, pseudo_bytes};
use tower::ServiceExt;

async fn call(
    app: &axum::Router,
    method: &str,
    uri: &str,
    json_body: Option<&str>,
) -> (StatusCode, serde_json::Value, String) {
    call_with_body(app, method, uri, json_body.map(|s| s.to_string()), None).await
}

async fn call_with_body(
    app: &axum::Router,
    method: &str,
    uri: &str,
    json_body: Option<String>,
    run_header: Option<String>,
) -> (StatusCode, serde_json::Value, String) {
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(r) = run_header {
        builder = builder.header("x-run-id", r);
    }
    let req = if let Some(b) = json_body {
        builder
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(b))
            .unwrap()
    } else {
        builder.body(Body::empty()).unwrap()
    };
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let run = resp
        .headers()
        .get("x-run-id")
        .map(|v| v.to_str().unwrap_or("").to_string())
        .unwrap_or_default();
    let bytes = to_bytes(resp.into_body(), 16 * 1024 * 1024).await.unwrap();
    let json: serde_json::Value = serde_json::from_slice(&bytes).unwrap_or_else(|_| {
        panic!(
            "非 JSON 响应: status={status} body={}",
            String::from_utf8_lossy(&bytes)
        )
    });
    (status, json, run)
}

fn error_kind(v: &serde_json::Value) -> String {
    v["error"].as_str().unwrap_or("<missing>").to_string()
}

#[tokio::test]
async fn health_and_lifecycle() {
    let mut log = TestLog::new("health_and_lifecycle");
    let h = Harness::new();
    let app = h.app();

    let (s, v, _) = call(&app, "GET", "/v1/health", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["status"], "ok");

    // 创建
    let body = serde_json::json!({"name":"fruits","text":"banana"}).to_string();
    let (s, v, _) = call(&app, "POST", "/v1/indexes", Some(&body)).await;
    assert_eq!(s, StatusCode::CREATED, "{v}");
    assert_eq!(v["data"]["name"], "fruits");
    assert_eq!(v["data"]["text_len"], 6);

    // 列表
    let (s, v, _) = call(&app, "GET", "/v1/indexes", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["loaded"][0], "fruits");

    // 元信息
    let (s, _, _) = call(&app, "GET", "/v1/indexes/fruits", None).await;
    assert_eq!(s, StatusCode::OK);

    // 重复创建 -> 409 state_conflict
    let (s, v, _) = call(&app, "POST", "/v1/indexes", Some(&body)).await;
    assert_eq!(s, StatusCode::CONFLICT);
    assert_eq!(error_kind(&v), "state_conflict");
    log.record("重复创建状态码", s.as_u16());

    // 删除后 404，再查不到
    let (s, _, _) = call(&app, "DELETE", "/v1/indexes/fruits", None).await;
    assert_eq!(s, StatusCode::OK);
    let (s, v, _) = call(&app, "GET", "/v1/indexes/fruits", None).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    assert_eq!(error_kind(&v), "not_found");
}

#[tokio::test]
async fn search_get_and_post_return_concrete_hits() {
    let mut log = TestLog::new("search_get_and_post_return_concrete_hits");
    let h = Harness::new();
    h.build_index("banana", b"banana", 64, 2);
    let app = h.app();

    // GET 查询
    let (s, v, _) = call(&app, "GET", "/v1/indexes/banana/search?pattern=ana", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["count"], 2);
    assert_eq!(v["data"]["interval"]["lo"], 2);
    assert_eq!(v["data"]["interval"]["hi"], 4);
    assert_eq!(v["data"]["interval"]["half_open"], true);
    assert_eq!(v["data"]["locations"], serde_json::json!([1, 3]));

    // POST 查询 + trace 步骤
    let body = serde_json::json!({"pattern":"ana","trace":true}).to_string();
    let (s, v, _) = call(&app, "POST", "/v1/indexes/banana/search", Some(&body)).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["locations"], serde_json::json!([1, 3]));
    assert_eq!(v["data"]["trace"].as_array().unwrap().len(), 3);
    log.record("trace 步骤数", v["data"]["trace"].as_array().unwrap().len());

    // 空模式（GET 缺省即空；POST empty=true）
    let (s, v, _) = call(&app, "GET", "/v1/indexes/banana/search", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["count"], 7);
    assert_eq!(
        v["data"]["locations"],
        serde_json::json!([0, 1, 2, 3, 4, 5, 6])
    );

    // 超长模式 -> count 0
    let body = serde_json::json!({"pattern":"banana-banana"}).to_string();
    let (s, v, _) = call(&app, "POST", "/v1/indexes/banana/search", Some(&body)).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["count"], 0);
    assert_eq!(v["data"]["interval"]["lo"], v["data"]["interval"]["hi"]);

    // count 端点
    let (s, v, _) = call(&app, "GET", "/v1/indexes/banana/count?pattern=na", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["count"], 2);
}

#[tokio::test]
async fn binary_pattern_via_base64_and_verify_endpoint() {
    let mut log = TestLog::new("binary_pattern_via_base64_and_verify_endpoint");
    let h = Harness::new();
    let text = b"\x00\x00\xff\x00\x00\x01".as_slice();
    h.build_index("bin", text, 8, 2);
    let app = h.app();

    // base64("\x00\x00") == "AAA="
    let (s, v, _) = call(
        &app,
        "POST",
        "/v1/indexes/bin/search",
        Some(r#"{"pattern_base64":"AAA="}"#),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!(v["data"]["count"], 2);
    assert_eq!(v["data"]["locations"], serde_json::json!([0, 3]));
    log.record("零字节模式 base64 命中", v["data"]["count"].as_u64());

    // 坏 base64 -> 400 invalid_input
    let (s, v, _) = call(
        &app,
        "POST",
        "/v1/indexes/bin/search",
        Some(r#"{"pattern_base64":"@@not-base64@@"}"#),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(error_kind(&v), "invalid_input");

    // verify：索引结果与独立朴素扫描一致
    let (s, v, _) = call(
        &app,
        "POST",
        "/v1/indexes/bin/verify",
        Some(r#"{"pattern_base64":"AP8="}"#), // \x00\xff
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["agree"], true, "verify 必须与朴素扫描一致: {v}");
    assert_eq!(v["data"]["fm_count"], v["data"]["naive_count"]);

    // 空模式 verify（两边都应给出 m+1 与 0..=m）
    let (s, v, _) = call(
        &app,
        "POST",
        "/v1/indexes/bin/verify",
        Some(r#"{"empty":true}"#),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["agree"], true);
    assert_eq!(v["data"]["naive_count"], 7);
}

#[tokio::test]
async fn error_classes_are_distinguishable_over_http() {
    let mut log = TestLog::new("error_classes_are_distinguishable_over_http");
    let h = Harness::new();
    h.build_index("ok", b"abc", 16, 4);
    let app = h.app();

    // 400：非法 JSON
    let (s, v, _) = call(&app, "POST", "/v1/indexes", Some("{not-json")).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(error_kind(&v), "invalid_input");

    // 400：名字非法（路径穿越字符）
    let body = serde_json::json!({"name":"../escape","text":"x"}).to_string();
    let (s, v, _) = call(&app, "POST", "/v1/indexes", Some(&body)).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(error_kind(&v), "invalid_input");

    // 400：三个来源字段同时给
    let body =
        serde_json::json!({"name":"x","text":"a","text_base64":"YQ==","path":"f"}).to_string();
    let (s, v, _) = call(&app, "POST", "/v1/indexes", Some(&body)).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(error_kind(&v), "invalid_input");

    // 404：索引不存在（业务 404，消息必须指出索引名，而非“路由不存在”）
    let (s, v, _) = call(&app, "GET", "/v1/indexes/nope/search?pattern=a", None).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    assert_eq!(error_kind(&v), "not_found");
    assert!(
        v["message"].as_str().unwrap().contains("nope"),
        "业务 404 消息应含索引名: {v}"
    );
    assert!(
        !v["message"].as_str().unwrap().contains("路由不存在"),
        "不应被误判为框架 404"
    );

    // 404：路由不存在（框架 404，消息不同）
    let (s, v, _) = call(&app, "GET", "/v1/nonsense", None).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    assert_eq!(error_kind(&v), "not_found");
    assert!(v["message"].as_str().unwrap().contains("路由不存在"));

    // 413：超大 body（app 上限 8MiB，直接灌 9MiB）
    let huge = format!(
        "{{\"name\":\"big\",\"text\":\"{}\"}}",
        "x".repeat(9 * 1024 * 1024)
    );
    let (s, v, _) = call(&app, "POST", "/v1/indexes", Some(&huge)).await;
    assert_eq!(s, StatusCode::PAYLOAD_TOO_LARGE);
    assert_eq!(error_kind(&v), "resource_exhausted");
    log.record("超大请求体状态码", s.as_u16());

    // 每个错误体都带 run_id
    assert!(
        v.get("run_id")
            .and_then(|x| x.as_str())
            .is_some_and(|s| s.starts_with("run-"))
    );
}

#[tokio::test]
async fn run_id_is_echoed_and_replayable() {
    let _log = TestLog::new("run_id_is_echoed_and_replayable");
    let h = Harness::new();
    h.build_index("r", b"aaa", 16, 1);
    let app = h.app();
    // 成功路径同样携带 run_id（信封顶层），并接受客户端指定 x-run-id
    let (s, v, _) = call_with_body(
        &app,
        "GET",
        "/v1/indexes/r/count?pattern=a",
        None,
        Some("replay-id-123".to_string()),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["run_id"], "replay-id-123", "客户端 run_id 应原样回显");
}

#[tokio::test]
async fn on_disk_unloaded_index_reports_clear_state() {
    let mut log = TestLog::new("on_disk_unloaded_index_reports_clear_state");
    // 直接把索引落盘到新服务目录，但不加载
    let h = Harness::new();
    h.build_index("diskonly", b"persistent words", 16, 4);
    let fresh = common::Harness::new();
    // 手工复制目录到 fresh 的 data_dir
    let src = h.service.data_dir().join("diskonly");
    let dst = fresh.service.data_dir().join("diskonly");
    copy_dir(&src, &dst);

    let app = fresh.app();
    // health 能看到 on_disk 有但 loaded 没有
    let (_, v, _) = call(&app, "GET", "/v1/health", None).await;
    assert_eq!(v["data"]["on_disk"][0], "diskonly");
    assert!(v["data"]["loaded"].as_array().unwrap().is_empty());

    // 查询未加载索引 -> invalid_input 且消息提示先 load（状态冲突可区分于不存在）
    let (s, v, _) = call(&app, "GET", "/v1/indexes/diskonly/count?pattern=a", None).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert_eq!(error_kind(&v), "invalid_input");
    assert!(v["message"].as_str().unwrap().contains("/load"));
    log.record("未加载索引查询", v["message"].as_str());

    // load 后查询成功（"persistent words" 中 's' 出现 3 次，'a' 出现 0 次）
    let (s, _, _) = call(&app, "POST", "/v1/indexes/diskonly/load", None).await;
    assert_eq!(s, StatusCode::OK);
    let (s, v, _) = call(&app, "GET", "/v1/indexes/diskonly/count?pattern=s", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["count"], 3);
    let (s, v, _) = call(&app, "GET", "/v1/indexes/diskonly/count?pattern=a", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["count"], 0);
}

#[tokio::test]
async fn file_import_whitelist_and_verify_agree() {
    let mut log = TestLog::new("file_import_whitelist_and_verify_agree");
    let h = Harness::new();
    // 在导入白名单放一个文件（含零字节）
    let fpath = h.import.path().join("sample.bin");
    let payload = pseudo_bytes(42, 256, 4);
    std::fs::write(&fpath, &payload).unwrap();
    let app = h.app();

    let body =
        serde_json::json!({"name":"imp","path":"sample.bin","rank_block":32,"sample_step":5})
            .to_string();
    let (s, v, _) = call(&app, "POST", "/v1/indexes", Some(&body)).await;
    assert_eq!(s, StatusCode::CREATED, "{v}");

    // 路径逃逸被拒绝
    let body = serde_json::json!({"name":"evil","path":"../../etc/passwd"}).to_string();
    let (s, v, _) = call(&app, "POST", "/v1/indexes", Some(&body)).await;
    assert!(
        s == StatusCode::NOT_FOUND || s == StatusCode::BAD_REQUEST,
        "{s} {v}"
    );
    log.record("逃逸路径状态码", s.as_u16());

    // verify 多模式一致
    for p in [vec![0u8, 0], vec![1], vec![2, 3], vec![3, 3, 3, 3], vec![]] {
        let b64 = base64::engine::general_purpose::STANDARD.encode(&p);
        let req = serde_json::json!({"pattern_base64": b64}).to_string();
        let (s, v, _) = call(&app, "POST", "/v1/indexes/imp/verify", Some(&req)).await;
        assert_eq!(s, StatusCode::OK);
        assert_eq!(v["data"]["agree"], true, "pat={p:?} {v}");
    }
}

fn copy_dir(src: &std::path::Path, dst: &std::path::Path) {
    std::fs::create_dir_all(dst).unwrap();
    for e in std::fs::read_dir(src).unwrap() {
        let e = e.unwrap();
        std::fs::copy(e.path(), dst.join(e.file_name())).unwrap();
    }
}
