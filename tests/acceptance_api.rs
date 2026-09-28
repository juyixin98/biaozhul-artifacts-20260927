//! 验收测试 4：HTTP 接口端到端。用 axum 自带的内存请求（oneshot），
//! 不绑定端口、不依赖外部进程。

use axum::body::Body;
use axum::http::{Request, StatusCode};
use cnf_dpll::api::build_router;
use cnf_dpll::config::AppConfig;
use tower::ServiceExt;

fn app() -> axum::Router {
    build_router(AppConfig::default())
}

async fn post(
    uri: &str,
    body: serde_json::Value,
) -> (StatusCode, serde_json::Value) {
    let resp = app()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(uri)
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(&body).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    let json: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    (status, json)
}

#[tokio::test]
async fn health_ok() {
    let resp = app()
        .oneshot(Request::builder().uri("/health").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
}

#[tokio::test]
async fn solve_dimacs_sat_returns_model_and_diagnostics() {
    let body = serde_json::json!({
        "dimacs": "p cnf 2 2\n1 2 0\n-1 2 0\n"
    });
    let (status, json) = post("/solve", body).await;
    assert_eq!(status, StatusCode::OK, "{json}");
    assert_eq!(json["accepted"], true);
    assert_eq!(json["outcome"]["result"], "SAT");
    let lits = json["outcome"]["model"]["true_literals"]
        .as_array()
        .unwrap();
    assert_eq!(lits.len(), 2);
    // x2 必为真（编码 4）。
    assert!(lits.iter().any(|v| v == 4));
    // 诊断带 request_id 与关键计数。
    assert!(json["diagnostics"]["request_id"].is_string());
    assert_eq!(json["diagnostics"]["num_vars"], 2);
    assert_eq!(json["diagnostics"]["num_clauses"], 2);
}

#[tokio::test]
async fn solve_dimacs_unsat_returns_independently_verifiable_proof() {
    let body = serde_json::json!({
        "dimacs": "p cnf 2 4\n1 2 0\n1 -2 0\n-1 2 0\n-1 -2 0\n"
    });
    let (status, json) = post("/solve", body).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(json["outcome"]["result"], "UNSAT");
    let proof = &json["outcome"]["proof"];
    assert!(proof["steps"].is_array());

    // 立即把同一公式与证明交给 /verify/proof：必须通过。
    let verify = serde_json::json!({
        "dimacs": "p cnf 2 4\n1 2 0\n1 -2 0\n-1 2 0\n-1 -2 0\n",
        "proof": proof,
    });
    let (vstatus, vjson) = post("/verify/proof", verify).await;
    assert_eq!(vstatus, StatusCode::OK);
    assert_eq!(vjson["accepted"], true);
    assert_eq!(vjson["verdict"], "PROOF_VALID");
}

#[tokio::test]
async fn solve_parse_error_has_specific_category() {
    let body = serde_json::json!({
        "dimacs": "p cnf 2 1\n1 3 0\n"
    });
    let (status, json) = post("/solve", body).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(json["accepted"], false);
    assert_eq!(json["error_category"], "parse_error");
    assert!(
        json["detail"].as_str().unwrap().contains("超过头部声明"),
        "诊断必须具体: {}",
        json["detail"]
    );
}

#[tokio::test]
async fn solve_empty_body_is_422_with_category() {
    let body = serde_json::json!({});
    let (status, json) = post("/solve", body).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(json["error_category"], "empty_request");
}

#[tokio::test]
async fn structured_clauses_work() {
    let body = serde_json::json!({
        "num_vars": 3,
        "clauses": [[1, -2], [2, 3], [-3]]
    });
    let (status, json) = post("/solve", body).await;
    assert_eq!(status, StatusCode::OK, "{json}");
    assert_eq!(json["outcome"]["result"], "SAT");
}

#[tokio::test]
async fn verify_model_endpoint_rejects_tampering_with_category() {
    // 先取一个合法模型，再翻转后送验证端点。
    let body = serde_json::json!({
        "dimacs": "p cnf 2 2\n1 2 0\n-1 2 0\n"
    });
    let (_, solved) = post("/solve", body).await;
    let mut model = solved["outcome"]["model"].clone();
    // 全部翻极性。
    let flipped: Vec<serde_json::Value> = model["true_literals"]
        .as_array()
        .unwrap()
        .iter()
        .map(|l| {
            let x = l.as_u64().unwrap() as u32;
            serde_json::json!(x ^ 1)
        })
        .collect();
    model["true_literals"] = serde_json::json!(flipped);

    let verify = serde_json::json!({
        "dimacs": "p cnf 2 2\n1 2 0\n-1 2 0\n",
        "model": model,
    });
    let (status, json) = post("/verify/model", verify).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(json["accepted"], false);
    assert_eq!(json["verdict"], "MODEL_INVALID");
    assert_eq!(json["failure_category"], "clause_not_satisfied");
}

#[tokio::test]
async fn request_id_is_echoed_when_provided() {
    let body = serde_json::json!({
        "request_id": "fixed-id-123",
        "dimacs": "p cnf 1 1\n1 0\n"
    });
    let (_, json) = post("/solve", body).await;
    assert_eq!(json["request_id"], "fixed-id-123");
    assert_eq!(json["diagnostics"]["request_id"], "fixed-id-123");
}

#[tokio::test]
async fn tight_budget_via_api_returns_unknown() {
    // PHP-4-3 配 max_decisions=0 必 UNKNOWN。
    let mut clauses: Vec<String> = Vec::new();
    let (p, h) = (4usize, 3usize);
    let lit = |i: usize, j: usize| (i as i64 - 1) * h as i64 + j as i64;
    let mut raw: Vec<Vec<i64>> = Vec::new();
    for i in 1..=p {
        raw.push((1..=h).map(|j| lit(i, j)).collect());
    }
    for i in 1..=p {
        for k in (i + 1)..=p {
            for j in 1..=h {
                raw.push(vec![-lit(i, j), -lit(k, j)]);
            }
        }
    }
    for c in &raw {
        let mut s = c.iter().map(|x| x.to_string()).collect::<Vec<_>>().join(" ");
        s.push_str(" 0");
        clauses.push(s);
    }
    let dimacs = format!(
        "p cnf {} {}\n{}\n",
        p * h,
        raw.len(),
        clauses.join("\n")
    );
    let body = serde_json::json!({
        "dimacs": dimacs,
        "max_decisions": 0,
        "max_propagations": 1,
    });
    let (status, json) = post("/solve", body).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(json["outcome"]["result"], "UNKNOWN");
    assert_eq!(json["diagnostics"]["budget_exhausted"], true);
}
