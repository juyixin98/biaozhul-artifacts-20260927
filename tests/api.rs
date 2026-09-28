//! HTTP 端到端测试：通过 axum 路由发真实 HTTP 请求，断言具体判定、失败类别、
//! request-id 关联、证据交叉验证，而非只检查“接口能调用”。

mod common;

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use petri_reach::api::build_router;
use petri_reach::config::Config;
use tower::ServiceExt;

fn app() -> axum::Router {
    build_router(Arc::new(Config::default()))
}

fn post_json(uri: &str, body: serde_json::Value) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap()
}

async fn send(req: Request<Body>) -> (StatusCode, serde_json::Value) {
    let resp = app().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap_or_else(|_| {
        panic!("response body must be JSON, got: {}", String::from_utf8_lossy(&bytes))
    });
    (status, value)
}

fn mutex_net() -> serde_json::Value {
    serde_json::json!({
        "places": [
            {"name": "free", "capacity": 1},
            {"name": "idle_a", "capacity": 1},
            {"name": "crit_a", "capacity": 1},
            {"name": "idle_b", "capacity": 1},
            {"name": "crit_b", "capacity": 1}
        ],
        "transitions": [
            {"name": "enter_a", "inputs": [{"place": "idle_a"}, {"place": "free"}], "outputs": [{"place": "crit_a"}]},
            {"name": "leave_a", "inputs": [{"place": "crit_a"}], "outputs": [{"place": "idle_a"}, {"place": "free"}]},
            {"name": "enter_b", "inputs": [{"place": "idle_b"}, {"place": "free"}], "outputs": [{"place": "crit_b"}]},
            {"name": "leave_b", "inputs": [{"place": "crit_b"}], "outputs": [{"place": "idle_b"}, {"place": "free"}]}
        ]
    })
}

#[tokio::test]
async fn health_reports_service_and_version() {
    let req = Request::builder().uri("/health").body(Body::empty()).unwrap();
    let resp = app().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let v: serde_json::Value =
        serde_json::from_slice(&axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap())
            .unwrap();
    assert_eq!(v["status"], "ok");
    assert_eq!(v["service"], "petri-reach");
    assert_eq!(v["version"], env!("CARGO_PKG_VERSION"));
}

#[tokio::test]
async fn reachable_returns_certificate_and_scope() {
    let body = serde_json::json!({
        "net": mutex_net(),
        "initial_marking": {"free": 1, "idle_a": 1, "idle_b": 1},
        "target_marking": {"crit_a": 1, "idle_b": 1}
    });
    let (status, v) = send(post_json("/api/v1/reachability", body)).await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["decision"], "reachable");
    assert_eq!(v["reachable"], true);
    assert_eq!(v["basis"], "bfs_explicit_path");
    assert_eq!(v["certificate"]["transition_sequence"], serde_json::json!(["enter_a"]));
    assert_eq!(v["certificate"]["path_length"], 1);
    // 完备性边界声明必须出现在每个响应中。
    assert!(v["scope"].as_str().unwrap().contains("capacity model"));
    assert!(v["net"]["state_space_upper_bound"].as_u64().unwrap() >= 1);
}

#[tokio::test]
async fn mutually_exclusive_target_is_unreachable_with_justification() {
    let body = serde_json::json!({
        "net": mutex_net(),
        "initial_marking": {"free": 1, "idle_a": 1, "idle_b": 1},
        "target_marking": {"crit_a": 1, "crit_b": 1}
    });
    let (status, v) = send(post_json("/api/v1/reachability", body)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["decision"], "unreachable");
    assert_eq!(v["reachable"], false);
    assert!(v["certificate"].is_null());
    // 互斥由资源 P 不变量否决。
    assert_eq!(v["basis"], "p_invariant_conservation_witness");
    let obs = &v["invariant_obstruction"];
    // 资源守恒律 [free,crit_a,crit_b]：初始 free=1 -> 加权和 1；
    // 目标 crit_a=1,crit_b=1 -> 加权和 2。守恒律下两者必须相等，不等即否决。
    assert_eq!(obs["weights"], serde_json::json!([1, 0, 1, 0, 1]));
    assert_eq!(obs["initial_weighted_sum"], 1);
    assert_eq!(obs["target_weighted_sum"], 2);
}

#[tokio::test]
async fn inconclusive_is_not_reported_as_success_or_unreachable() {
    // 容量 100 自增，极小 state_limit，目标遥远：必须 inconclusive。
    let body = serde_json::json!({
        "net": {
            "places": [{"name": "p", "capacity": 100}],
            "transitions": [{"name": "inc", "outputs": [{"place": "p"}]}]
        },
        "initial_marking": {},
        "target_marking": {"p": 90},
        "state_limit": 5
    });
    let (status, v) = send(post_json("/api/v1/reachability", body)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["decision"], "inconclusive");
    assert_eq!(v["reachable"], false);
    assert_eq!(v["basis"], "state_limit_reached");
    assert!(v["certificate"].is_null());
}

#[tokio::test]
async fn input_failures_return_distinct_codes_not_200() {
    let cases: &[(serde_json::Value, &str)] = &[
        // 未知库所。
        (
            serde_json::json!({"net": mutex_net(), "initial_marking": {"nope": 1}, "target_marking": {}}),
            "unknown_place",
        ),
        // 目标超容量。
        (
            serde_json::json!({"net": mutex_net(), "initial_marking": {"free": 1}, "target_marking": {"free": 2}}),
            "token_exceeds_capacity",
        ),
        // 弧引用未知库所（网本身非法）。
        (
            serde_json::json!({
                "net": {"places": [{"name":"p","capacity":1}], "transitions": [{"name":"t","inputs":[{"place":"q"}]}]},
                "initial_marking": {}, "target_marking": {}
            }),
            "unknown_place",
        ),
        // 0 权弧。
        (
            serde_json::json!({
                "net": {"places": [{"name":"p","capacity":1}], "transitions": [{"name":"t","outputs":[{"place":"p","weight":0}]}]},
                "initial_marking": {}, "target_marking": {}
            }),
            "nonpositive_weight",
        ),
    ];
    for (body, code) in cases {
        let (status, v) = send(post_json("/api/v1/reachability", body.clone())).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "body {body}");
        assert_eq!(v["error"].as_str(), Some(*code), "body {body}, got {v}");
    }

    // 非法 JSON 是 parse/invalid_json 类，同样不得成功。
    let req = Request::builder()
        .method("POST")
        .uri("/api/v1/reachability")
        .header("content-type", "application/json")
        .body(Body::from("{not json"))
        .unwrap();
    let (status, v) = send(req).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(v["error"], "invalid_json");
}

#[tokio::test]
async fn verify_firing_accepts_valid_evidence_and_cross_checks_kernel() {
    let body = serde_json::json!({
        "net": mutex_net(),
        "initial_marking": {"free": 1, "idle_a": 1, "idle_b": 1},
        "transition_sequence": ["enter_a", "leave_a"],
        "claimed_final_marking": {"free": 1, "idle_a": 1, "idle_b": 1}
    });
    let (status, v) = send(post_json("/api/v1/verify/firing", body)).await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["valid"], true);
    assert_eq!(v["kernel_agrees"], true);
    assert_eq!(v["replay"]["steps_fired"], 2);
    assert_eq!(v["claimed_final_matches"], true);
    // 逐步轨迹包含 M0..M2。
    assert_eq!(v["replay"]["reached"].as_array().unwrap().len(), 3);
}

#[tokio::test]
async fn verify_firing_rejects_illegal_step_with_category_422() {
    // free 在 enter_a 后已被占用，再 enter_b 缺 free：独立重放必须在第 1 步失败。
    let body = serde_json::json!({
        "net": mutex_net(),
        "initial_marking": {"free": 1, "idle_a": 1, "idle_b": 1},
        "transition_sequence": ["enter_a", "enter_b"]
    });
    let (status, v) = send(post_json("/api/v1/verify/firing", body)).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["error"], "firing_evidence_invalid");
    assert_eq!(v["details"]["index"], 1);
    assert_eq!(v["details"]["transition"], "enter_b");
    assert!(v["details"]["deficits"].as_array().unwrap().iter().any(|d| d["place"] == "free"));
}

#[tokio::test]
async fn verify_firing_detects_claimed_final_mismatch() {
    let body = serde_json::json!({
        "net": mutex_net(),
        "initial_marking": {"free": 1, "idle_a": 1, "idle_b": 1},
        "transition_sequence": ["enter_a"],
        "claimed_final_marking": {"idle_a": 1, "idle_b": 1, "free": 1} // 实际应是 crit_a
    });
    let (status, v) = send(post_json("/api/v1/verify/firing", body)).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(v["error"], "final_marking_mismatch");
}

#[tokio::test]
async fn verify_invariant_validates_candidate() {
    // [free, idle_a, crit_a, idle_b, crit_b]：资源守恒律 [1,0,1,0,1]。
    let body = serde_json::json!({
        "net": mutex_net(),
        "initial_marking": {"free": 1, "idle_a": 1, "idle_b": 1},
        "target_marking": {"crit_a": 1, "idle_b": 1},
        "weights": {"free": 1, "crit_a": 1, "crit_b": 1}
    });
    let (status, v) = send(post_json("/api/v1/verify/invariant", body)).await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["report"]["valid"], true);
    assert_eq!(v["report"]["residual_max_abs"], 0);
    assert_eq!(v["report"]["weighted_sum_matches"], true);

    // 非守恒候选：只给 free 权重 1。
    let body2 = serde_json::json!({
        "net": mutex_net(),
        "weights": {"free": 1}
    });
    let (s2, v2) = send(post_json("/api/v1/verify/invariant", body2)).await;
    assert_eq!(s2, StatusCode::OK);
    assert_eq!(v2["report"]["valid"], false);
    assert!(v2["report"]["residual_max_abs"].as_i64().unwrap() > 0);
}

#[tokio::test]
async fn invariants_endpoint_lists_candidate() {
    let body = serde_json::json!({"net": mutex_net()});
    let (status, v) = send(post_json("/api/v1/invariants", body)).await;
    assert_eq!(status, StatusCode::OK, "{v}");
    let weights: Vec<serde_json::Value> = v["report"]["candidates"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["weights"].clone())
        .collect();
    assert!(weights.iter().any(|w| w == &serde_json::json!([1, 0, 1, 0, 1])));
}

#[tokio::test]
async fn pnet_text_input_is_accepted() {
    let pnet = std::fs::read_to_string(
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("fixtures/mutex.pnet"),
    )
    .unwrap();
    let body = serde_json::json!({
        "net": serde_json::Value::Null,
        "net_text": pnet,
        "initial_marking": {"free": 1, "idle_a": 1, "idle_b": 1},
        "target_marking": {"crit_a": 1, "idle_b": 1}
    });
    let (status, v) = send(post_json("/api/v1/reachability", body)).await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["decision"], "reachable");
}

#[tokio::test]
async fn request_id_is_echoed_and_generated_when_absent() {
    let body = serde_json::json!({
        "net": mutex_net(),
        "initial_marking": {"free": 1, "idle_a": 1, "idle_b": 1},
        "target_marking": {"crit_a": 1, "idle_b": 1}
    });
    // 客户端提供 id：原样回显且进入响应体。
    let mut req = post_json("/api/v1/reachability", body.clone());
    req.headers_mut().insert("x-request-id", "run-42".parse().unwrap());
    let resp = app().oneshot(req).await.unwrap();
    assert_eq!(resp.headers().get("x-request-id").unwrap(), "run-42");
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["request_id"], "run-42");

    // 不提供：生成非空 id。
    let resp2 = app()
        .oneshot(post_json("/api/v1/reachability", body))
        .await
        .unwrap();
    let rid = resp2.headers().get("x-request-id").unwrap().to_str().unwrap().to_string();
    assert!(!rid.is_empty());
    assert_ne!(rid, "run-42");
}

#[tokio::test]
async fn oversized_body_is_rejected() {
    // 默认 body_limit 1MiB；塞一个超过上限的网描述。
    let big = "x".repeat(1_200_000);
    let req = Request::builder()
        .method("POST")
        .uri("/api/v1/reachability")
        .header("content-type", "application/json")
        .body(Body::from(format!("{{\"padding\":\"{big}\"}}")))
        .unwrap();
    let resp = app().oneshot(req).await.unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::PAYLOAD_TOO_LARGE,
        "oversized body must be rejected before reaching the solver"
    );
}
