//! HTTP API 端到端测试：用 axum 内存路由 + tower::ServiceExt 直接发请求，
//! 不绑定真实端口。断言具体结论、request_id 关联、预算语义与篡改拒绝类别。

use cnf_solver_backend::api::{router, AppState};
use cnf_solver_backend::config::Config;

use serde_json::{json, Value};
use std::sync::Arc;
use tower::ServiceExt;

fn app() -> axum::Router {
    let config = Config {
        bind: "127.0.0.1:0".into(),
        max_decisions: Some(100_000),
        time_limit: Some(std::time::Duration::from_secs(5)),
        json_logs: false,
    };
    router(AppState {
        config: Arc::new(config),
    })
}

async fn post(path: &str, body: Value) -> (hyper::StatusCode, Value) {
    let resp = app()
        .oneshot(
            axum::http::Request::post(path)
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
    let value: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, value)
}

#[tokio::test]
async fn healthz_ok() {
    let resp = app()
        .oneshot(
            axum::http::Request::get("/healthz")
                .body(axum::body::Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), hyper::StatusCode::OK);
}

#[tokio::test]
async fn sat_request_returns_accepted_model_with_request_id() {
    let (status, body) = post(
        "/solve",
        json!({
            "request_id": "req-sat-1",
            "num_vars": 3,
            "clauses": [[1, -2], [2, 3]]
        }),
    )
    .await;
    assert_eq!(status, hyper::StatusCode::OK);
    assert_eq!(body["request_id"], "req-sat-1");
    assert_eq!(body["verdict"], "sat");
    assert_eq!(body["evidence_check"]["result"], "accepted");
    assert_eq!(body["evidence_check"]["what"], "model");
    let model = body["model"].as_array().unwrap();
    assert_eq!(model.len(), 3); // 每个变量恰好一个有符号文字
}

#[tokio::test]
async fn unsat_dimacs_request_returns_verified_proof() {
    let (status, body) = post(
        "/solve",
        json!({
            "dimacs": "c classic unsat\np cnf 2 4\n1 2 0\n1 -2 0\n-1 2 0\n-1 -2 0\n"
        }),
    )
    .await;
    assert_eq!(status, hyper::StatusCode::OK);
    assert_eq!(body["verdict"], "unsat");
    assert_eq!(body["evidence_check"]["result"], "accepted");
    assert_eq!(body["evidence_check"]["what"], "resolution_proof");
    assert!(body["proof"]["derived_clauses"].is_array());
    assert!(body["request_id"].is_string()); // 未提供时自动生成
}

#[tokio::test]
async fn empty_clause_is_unsat_with_empty_derivation() {
    let (status, body) = post("/solve", json!({ "num_vars": 1, "clauses": [[]] })).await;
    assert_eq!(status, hyper::StatusCode::OK);
    assert_eq!(body["verdict"], "unsat");
    assert_eq!(body["proof"]["derived_clauses"], json!([]));
    assert!(body["proof"]["empty_clause_ref"]
        .as_str()
        .unwrap()
        .starts_with('i'));
}

#[tokio::test]
async fn bad_input_is_400_with_category() {
    let (status, body) = post(
        "/solve",
        json!({ "num_vars": 2, "clauses": [[3]] }), // 变量越界
    )
    .await;
    assert_eq!(status, hyper::StatusCode::BAD_REQUEST);
    assert_eq!(body["category"], "invalid_input");
    assert!(body["error"].as_str().unwrap().contains("variable"));
}

#[tokio::test]
async fn decision_budget_returns_unknown_not_unsat() {
    // 没有任何单位子句，结论必须经过真正的分支搜索；0 次决策预算 ⇒ UNKNOWN。
    // 预言机视角该公式确有满足解，但预算内无法确认，因此绝不能报 UNSAT/SAT。
    let (status, body) = post(
        "/solve",
        json!({
            "num_vars": 3,
            "clauses": [[1, 2, 3], [-1, 2], [-2, 3]],
            "limits": { "max_decisions": 0 }
        }),
    )
    .await;
    assert_eq!(status, hyper::StatusCode::OK);
    assert_eq!(body["verdict"], "unknown");
    assert!(body["conclusion"].as_str().unwrap().contains("UNKNOWN"));
    assert!(body["proof"].is_null());
    assert_eq!(body["diagnostics"]["stop_reason"], "decision_budget");
}

#[tokio::test]
async fn normalization_notes_are_reported() {
    let (status, body) = post("/solve", json!({ "clauses": [[1, 1, -2], [3, -3], []] })).await;
    assert_eq!(status, hyper::StatusCode::OK);
    let kinds: Vec<&str> = body["normalization"]
        .as_array()
        .unwrap()
        .iter()
        .map(|n| n["kind"].as_str().unwrap())
        .collect();
    assert!(kinds.contains(&"duplicate_lits_removed"));
    assert!(kinds.contains(&"tautology_dropped"));
    assert!(kinds.contains(&"empty_clause"));
    assert_eq!(body["verdict"], "unsat");
}

#[tokio::test]
async fn malformed_json_is_400_with_category_and_id() {
    let resp = app()
        .oneshot(
            axum::http::Request::post("/solve")
                .header("content-type", "application/json")
                .body(axum::body::Body::from("{ not json"))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), hyper::StatusCode::BAD_REQUEST);
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(body["category"], "malformed_json");
    assert!(body["request_id"].is_string());
}

#[tokio::test]
async fn unknown_field_is_400_invalid_input() {
    let (status, body) = post("/solve", json!({ "clauses": [[1]], "bogus": 1 })).await;
    assert_eq!(status, hyper::StatusCode::BAD_REQUEST);
    assert_eq!(body["category"], "malformed_json");
}

#[tokio::test]
async fn zero_time_limit_yields_unknown_for_decision_needed_formula() {
    // elapsed >= 0 恒真：任何需要决策的公式都必须立即 UNKNOWN，而不是 UNSAT。
    let (status, body) = post(
        "/solve",
        json!({
            "num_vars": 4,
            "clauses": [[1, 2, 3, 4], [-1, 2], [-2, 3], [-3, 4]],
            "limits": { "time_limit_ms": 0 }
        }),
    )
    .await;
    assert_eq!(status, hyper::StatusCode::OK);
    assert_eq!(body["verdict"], "unknown");
    assert!(body["proof"].is_null());
    assert_eq!(body["diagnostics"]["stop_reason"], "time_budget");
    assert!(body["conclusion"].as_str().unwrap().contains("UNKNOWN"));
}

#[tokio::test]
async fn zero_time_limit_still_allows_tiny_formulas() {
    // 0ms 预算：第 0 层直接 UNSAT（无需决策、几乎不耗时）仍可完成；
    // 这保证“预算”只约束搜索，不误杀平凡结论。
    let (status, body) = post(
        "/solve",
        json!({ "clauses": [[1], [-1]], "limits": { "time_limit_ms": 0 } }),
    )
    .await;
    assert_eq!(status, hyper::StatusCode::OK);
    assert_eq!(body["verdict"], "unsat");
}

#[tokio::test]
async fn verify_endpoint_rejects_tampered_model_with_category() {
    let (status, body) = post(
        "/verify",
        json!({
            "num_vars": 3,
            "clauses": [[1, -2], [2, 3]],
            "model": [-1, -2, -3] // 第二条子句不满足
        }),
    )
    .await;
    assert_eq!(status, hyper::StatusCode::OK);
    assert_eq!(body["accepted"], false);
    assert_eq!(body["error"]["kind"], "clause_unsatisfied");
    assert_eq!(body["error"]["clause_index"], json!(1));
}

#[tokio::test]
async fn verify_endpoint_accepts_genuine_proof_and_rejects_tampered_pivot() {
    // 先合法求解拿到真实证明。
    let (_, solved) = post(
        "/solve",
        json!({ "clauses": [[1, 2], [1, -2], [-1, 3], [-1, -3]] }),
    )
    .await;
    assert_eq!(solved["verdict"], "unsat");
    let proof = solved["proof"].clone();

    let (status, body) = post(
        "/verify",
        json!({
            "clauses": [[1, 2], [1, -2], [-1, 3], [-1, -3]],
            "proof": proof
        }),
    )
    .await;
    assert_eq!(status, hyper::StatusCode::OK);
    assert_eq!(body["accepted"], true);

    // 篡改枢轴变量编号。
    let mut bogus = solved["proof"].clone();
    bogus["derived_clauses"][0]["resolvents"][0]["pivot_var"] = json!(99);
    let (_, body) = post(
        "/verify",
        json!({
            "clauses": [[1, 2], [1, -2], [-1, 3], [-1, -3]],
            "proof": bogus
        }),
    )
    .await;
    assert_eq!(body["accepted"], false);
    assert_eq!(body["error"]["kind"], "bad_pivot"); // 枢轴变量 99 不在消解子句中
}
