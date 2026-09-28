//! 后端接口集成测试：经真实 HTTP 栈（tower::ServiceExt::oneshot）断言
//! 状态码、错误类别、run_id、判定结论与证据字段。

use axum::body::Body;
use axum::http::{Request, StatusCode};
use tower::ServiceExt;

use weak_trace_inclusion::service::router;

async fn post_check(body: serde_json::Value) -> (StatusCode, serde_json::Value) {
    let req = Request::builder()
        .method("POST")
        .uri("/check")
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(&body).unwrap()))
        .unwrap();
    let resp = router().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 64 * 1024 * 1024)
        .await
        .unwrap();
    let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    (status, value)
}

fn extra_output_body() -> serde_json::Value {
    serde_json::json!({
        "run_id": "http-case-1",
        "observable_actions": ["a", "b"],
        "specification": {
            "name": "spec",
            "states": ["s0", "s1"],
            "initial_states": ["s0"],
            "edges": [{ "source": "s0", "action": "a", "target": "s1" }]
        },
        "implementation": {
            "name": "impl",
            "states": ["i0", "i1", "i2"],
            "initial_states": ["i0"],
            "hidden_actions": ["tau"],
            "edges": [
                { "source": "i0", "action": "tau", "target": "i0" },
                { "source": "i0", "action": "a", "target": "i1" },
                { "source": "i1", "action": "b", "target": "i2" }
            ]
        }
    })
}

#[tokio::test]
async fn health_ok() {
    let req = Request::builder()
        .method("GET")
        .uri("/health")
        .body(Body::empty())
        .unwrap();
    let resp = router().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
}

#[tokio::test]
async fn counterexample_response_is_full_contract() {
    let (status, v) = post_check(extra_output_body()).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["verdict"], "counterexample");

    // 运行编号：服务端 uuid + 客户端 run_id 回显。
    assert!(v["run_id"].as_str().unwrap().len() >= 32);
    assert_eq!(v["client_run_id"], "http-case-1");

    // 最短反例 [a,b]。
    assert_eq!(v["counterexample"]["trace"], serde_json::json!(["a", "b"]));
    assert_eq!(v["counterexample"]["length"], 2);
    assert_eq!(v["counterexample"]["shortest"], true);
    assert!(
        v["counterexample"]["specification_reachable"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        v["counterexample"]["implementation_reachable"],
        serde_json::json!(["i2"])
    );

    // 独立证据验证。
    assert_eq!(v["evidence_report"]["accepted"], true);
    let checks = v["evidence_report"]["checks"].as_array().unwrap();
    let names: Vec<&str> = checks.iter().map(|c| c["name"].as_str().unwrap()).collect();
    assert!(names.contains(&"impl_accepts_trace"));
    assert!(names.contains(&"spec_rejects_trace"));
    assert!(names.contains(&"impl_witness_walk_consistent"));
    for c in checks {
        assert_eq!(c["passed"], true, "证据检查项 {c} 应全部通过");
    }

    // 诊断：关键中间状态齐全。
    let d = &v["diagnostics"];
    assert_eq!(d["alphabet_size"], 2);
    assert!(d["search_nodes_visited"].as_u64().unwrap() >= 1);
    assert!(d["spec_closure_pairs"].as_u64().unwrap() >= 2);
    assert!(d["limits"]["max_search_nodes"].as_u64().unwrap() > 0);
    assert!(v["elapsed_ms"].as_u64().is_some());
}

#[tokio::test]
async fn included_response() {
    let mut body = extra_output_body();
    body["specification"]["edges"].as_array_mut().unwrap().push(
        serde_json::json!({ "source": "s1", "action": "b", "target": "s1" }),
    );
    let (status, v) = post_check(body).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["verdict"], "included");
    assert!(v.get("counterexample").is_none());
}

#[tokio::test]
async fn unknown_response_is_distinct_verdict() {
    let mut body = extra_output_body();
    body["limits"] = serde_json::json!({ "max_search_nodes": 1 });
    let (status, v) = post_check(body).await;
    assert_eq!(status, StatusCode::OK, "资源耗尽是判定结论，不是 HTTP 错误");
    assert_eq!(v["verdict"], "unknown");
    assert_eq!(v["unknown"]["reason_code"], "search_node_limit");
    assert!(v["unknown"]["policy"].as_str().unwrap().contains("不得解释为成立"));
}

#[tokio::test]
async fn malformed_json_is_400_with_run_id() {
    let req = Request::builder()
        .method("POST")
        .uri("/check")
        .header("content-type", "application/json")
        .body(Body::from("{ not json"))
        .unwrap();
    let resp = router().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(v["category"], "input_error");
    assert_eq!(v["code"], "malformed_json");
    assert!(v["run_id"].is_string());
}

#[tokio::test]
async fn missing_field_is_400() {
    let body = serde_json::json!({ "observable_actions": ["a"] });
    let (status, v) = post_check(body).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(v["category"], "input_error");
    assert_eq!(v["code"], "invalid_request_shape");
}

#[tokio::test]
async fn unknown_action_is_409_state_conflict() {
    let mut body = extra_output_body();
    body["implementation"]["edges"][1]["action"] = "z".into();
    let (status, v) = post_check(body).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(v["category"], "state_conflict");
    assert_eq!(v["code"], "unknown_action");
}

#[tokio::test]
async fn empty_alphabet_is_400() {
    let mut body = extra_output_body();
    body["observable_actions"] = serde_json::json!([]);
    let (status, v) = post_check(body).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(v["code"], "empty_observable_alphabet");
}
