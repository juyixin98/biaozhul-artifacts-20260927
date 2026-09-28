//! HTTP 后端集成测试：通过真实 Axum 路由器（tower::oneshot）走完整 JSON 协议，
//! 断言具体状态码、错误类别、节点数与见证内容——不只是“接口能调用”。

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use robdd::{build_router, AppState};
use serde_json::{json, Value};
use tower::util::ServiceExt;

fn app() -> axum::Router {
    build_router(AppState::new(), 20)
}

async fn call(
    router: &axum::Router,
    method: &str,
    uri: &str,
    body: Option<Value>,
    request_id: Option<&str>,
) -> (StatusCode, Value, String) {
    let builder = Request::builder().method(method).uri(uri);
    let builder = if let Some(rid) = request_id {
        builder.header("x-request-id", rid)
    } else {
        builder
    };
    let req = match body {
        Some(b) => builder
            .header("content-type", "application/json")
            .body(Body::from(serde_json::to_vec(&b).unwrap()))
            .unwrap(),
        None => builder.body(Body::empty()).unwrap(),
    };
    let resp = router.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let rid_header = resp
        .headers()
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let value: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, value, rid_header)
}

fn post(uri: &str, body: Value) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(&body).unwrap()))
        .unwrap()
}

#[tokio::test]
async fn health_reports_managers_and_echoes_request_id() {
    let router = app();
    let (status, body, rid) = call(&router, "GET", "/healthz", None, Some("trace-abc-123")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(rid, "trace-abc-123");
    assert_eq!(body["data"]["status"], json!("ok"));
    assert_eq!(body["request_id"], json!("trace-abc-123"));
}

#[tokio::test]
async fn full_workflow_build_apply_restrict_evaluate_and_node_counts() {
    let router = app();

    // 创建管理器。
    let (s, v, _rid) = call(
        &router,
        "POST",
        "/v1/managers",
        Some(json!({ "variable_order": ["a", "b", "c"] })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    let mid = v["data"]["manager_id"].as_u64().unwrap();

    // build 文本语法：a & b，应注册为根。
    let (s, v, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{mid}/build"),
        Some(json!({ "expr": "a & b", "root_name": "ab" })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    let edge_ab = v["data"]["edge"].as_str().unwrap().to_string();
    assert!(v["data"]["live_nodes"].as_u64().unwrap() >= 2);

    // 等价语法（De Morgan）规范边相同。
    let (s, v, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{mid}/build"),
        Some(json!({ "expr": "!(!a | !b)" })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["edge"].as_str().unwrap(), edge_ab);

    // JSON AST 也接受。
    let (s, _, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{mid}/build"),
        Some(json!({ "expr_json": { "Xor": [ {"Var":"a"}, {"Var":"b"} ] } })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);

    // apply：ab & c。
    let (s, v, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{mid}/build"),
        Some(json!({ "expr": "c" })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    let edge_c = v["data"]["edge"].as_str().unwrap().to_string();
    let (s, v, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{mid}/apply"),
        Some(json!({ "op": "and", "a": edge_ab, "b": edge_c, "root_name": "abc" })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    let edge_abc = v["data"]["edge"].as_str().unwrap().to_string();

    // evaluate 具体赋值。
    for (a, b, c, want) in [
        (true, true, true, true),
        (true, true, false, false),
        (true, false, true, false),
        (false, true, true, false),
    ] {
        let (s, v, _rid) = call(
            &router,
            "POST",
            &format!("/v1/managers/{mid}/evaluate"),
            Some(json!({ "edge": edge_abc, "assignment": {"a": a, "b": b, "c": c} })),
            None,
        )
        .await;
        assert_eq!(s, StatusCode::OK);
        assert_eq!(v["data"]["value"], json!(want), "a={a} b={b} c={c}");
    }

    // restrict a=true：ab&c 变成 b&c。
    let (s, v, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{mid}/restrict"),
        Some(json!({ "edge": edge_abc, "values": {"a": true} })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    let restricted = v["data"]["edge"].as_str().unwrap().to_string();
    let (s, v, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{mid}/evaluate"),
        Some(json!({ "edge": restricted, "assignment": {"b": true, "c": true} })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["value"], json!(true));
}

#[tokio::test]
async fn equivalence_endpoint_cross_checks_kernel_and_oracle_with_witness() {
    let router = app();

    // 等价：不同语法。
    let (s, v, _rid) = call(
        &router,
        "POST",
        "/v1/equivalence",
        Some(json!({
            "left_expr": "a & (b | c)",
            "right_expr": "(a & b) | (a & c)"
        })),
        Some("eq-1"),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!(v["request_id"], json!("eq-1"));
    assert_eq!(v["data"]["verdict"], json!("equivalent"));
    assert_eq!(v["data"]["cross_check"]["canonical_equal"], json!(true));
    assert_eq!(
        v["data"]["cross_check"]["truth_table_equivalent"],
        json!(true)
    );
    assert_eq!(
        v["data"]["result"]["verdict"],
        json!("equivalent"),
        "oracle verdict present"
    );

    // 不等价：具体见证。
    let (s, v, _rid) = call(
        &router,
        "POST",
        "/v1/equivalence",
        Some(json!({
            "left_expr": "a & (b | c)",
            "right_expr": "a | (b & c)"
        })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["verdict"], json!("not_equivalent"));
    assert_eq!(v["data"]["cross_check"]["canonical_equal"], json!(false));
    let witness = &v["data"]["result"]["witness"];
    assert!(witness["assignment"].is_object());
    assert_ne!(
        witness["left_value"], witness["right_value"],
        "witness distinguishes"
    );

    // 变量重命名后等价。
    let (s, v, _rid) = call(
        &router,
        "POST",
        "/v1/equivalence",
        Some(json!({
            "left_expr": "x & y -> z",
            "right_expr": "p & q -> r",
            "mapping": {"x": "p", "y": "q", "z": "r"}
        })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{v}");
    assert_eq!(v["data"]["verdict"], json!("equivalent"));

    // 非双射映射 → 拒绝（422 mapping_rejected）。
    let (s, v, _rid) = call(
        &router,
        "POST",
        "/v1/equivalence",
        Some(json!({
            "left_expr": "x & y",
            "right_expr": "p & q & r",
            "mapping": {"x": "p", "y": "q"}
        })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["error"]["code"], json!("mapping_rejected"));
    assert!(v["request_id"].is_string());
}

#[tokio::test]
async fn rejects_unknown_variable_parse_error_and_malformed_token() {
    let router = app();
    let (s, _, _rid) = call(
        &router,
        "POST",
        "/v1/managers",
        Some(json!({ "variable_order": ["a"] })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    // 第一个管理器 id 不是固定值——再建一个并使用它。
    let (s, v, _rid) = call(
        &router,
        "POST",
        "/v1/managers",
        Some(json!({ "variable_order": ["a", "b"] })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    let mid = v["data"]["manager_id"].as_u64().unwrap();

    // 未知变量。
    let (s, v, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{mid}/build"),
        Some(json!({ "expr": "a & nope" })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["error"]["code"], json!("unknown_variable"));

    // 语法错误。
    let (s, v, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{mid}/build"),
        Some(json!({ "expr": "a &" })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["error"]["code"], json!("parse_failed"));

    // 先拿一条合法边，再伪造坏令牌。
    let (_s, _v, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{mid}/build"),
        Some(json!({ "expr": "a" })),
        None,
    )
    .await;
    let (s, v, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{mid}/evaluate"),
        Some(json!({ "edge": "not-a-token", "assignment": {"a": true} })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["error"]["code"], json!("malformed_edge_token"));
}

#[tokio::test]
async fn cross_manager_edges_are_rejected_over_http() {
    let router = app();
    let mut edges = Vec::new();
    for order in [["a", "b"], ["a", "b"]] {
        let (_, v, _rid) = call(
            &router,
            "POST",
            "/v1/managers",
            Some(json!({ "variable_order": order })),
            None,
        )
        .await;
        let mid = v["data"]["manager_id"].as_u64().unwrap();
        let (_, v, _rid) = call(
            &router,
            "POST",
            &format!("/v1/managers/{mid}/build"),
            Some(json!({ "expr": "a & b" })),
            None,
        )
        .await;
        edges.push((mid, v["data"]["edge"].as_str().unwrap().to_string()));
    }
    let (m1, e1) = edges[0].clone();
    let (_m2, e2) = edges[1].clone();

    // 把管理器 2 的边发给管理器 1：边归属与路由管理器不一致，
    // 返回 422 foreign_manager（在内核入口之前就被后端防线拦下）。
    let (s, v, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{m1}/apply"),
        Some(json!({ "op": "and", "a": e1, "b": e2 })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY, "{v}");
    assert_eq!(v["error"]["code"], json!("foreign_manager"));
}

#[tokio::test]
async fn gc_keeps_roots_and_dangling_edges_return_410() {
    let router = app();
    let (_, v, _rid) = call(
        &router,
        "POST",
        "/v1/managers",
        Some(json!({ "variable_order": ["a", "b", "c"] })),
        None,
    )
    .await;
    let mid = v["data"]["manager_id"].as_u64().unwrap();

    let (_, v, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{mid}/build"),
        Some(json!({ "expr": "a & b", "root_name": "keep" })),
        None,
    )
    .await;
    let keep = v["data"]["edge"].as_str().unwrap().to_string();

    let (_, v, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{mid}/build"),
        Some(json!({ "expr": "a ^ b ^ c" })),
        None,
    )
    .await;
    let garbage = v["data"]["edge"].as_str().unwrap().to_string();

    let (s, v, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{mid}/gc"),
        None,
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert!(v["data"]["report"]["swept"].as_u64().unwrap() >= 1);

    // 根还在，可求值。
    let (s, v, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{mid}/evaluate"),
        Some(json!({ "edge": keep, "assignment": {"a": true, "b": true, "c": false} })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["data"]["value"], json!(true));

    // 垃圾边返回 410 Gone + reclaimed_node。
    let (s, v, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{mid}/evaluate"),
        Some(json!({ "edge": garbage, "assignment": {"a": true, "b": true, "c": true} })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::GONE);
    assert_eq!(v["error"]["code"], json!("reclaimed_node"));
}

#[tokio::test]
async fn sensitive_request_redacts_expression_from_error_text() {
    let router = app();
    let (_, v, _rid) = call(
        &router,
        "POST",
        "/v1/managers",
        Some(json!({ "variable_order": ["a"] })),
        None,
    )
    .await;
    let mid = v["data"]["manager_id"].as_u64().unwrap();

    let secret = "secret_password_var & a";
    let (s, v, _rid) = call(
        &router,
        "POST",
        &format!("/v1/managers/{mid}/build"),
        Some(json!({ "expr": secret, "sensitive": true })),
        None,
    )
    .await;
    assert_eq!(s, StatusCode::UNPROCESSABLE_ENTITY);
    // 错误消息不得包含敏感原文。
    let rendered = serde_json::to_string(&v).unwrap();
    assert!(!rendered.contains("secret_password_var"));
    assert_eq!(v["error"]["code"], json!("unknown_variable"));
}

#[tokio::test]
async fn request_id_is_generated_when_absent_and_present_on_errors() {
    let router = app();
    let resp = router
        .oneshot(post(
            "/v1/equivalence",
            json!({ "left_expr": "a &", "right_expr": "a" }),
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
    let rid = resp
        .headers()
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .unwrap()
        .to_string();
    assert!(rid.starts_with("req-"));
    let body = resp.into_body().collect().await.unwrap().to_bytes();
    let v: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(v["request_id"], json!(rid));
}
