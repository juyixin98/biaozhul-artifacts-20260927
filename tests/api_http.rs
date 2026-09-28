//! HTTP 层端到端测试：错误类别、原子批次、证据端到端、并发与 run-id。
//!
//! 这些用例通过 oneshot 驱动真实 axum 路由，断言具体的状态码、
//! `error.category`、版本号与数值，而不仅是“接口能调用”。

mod common;

use std::collections::BTreeMap;

use axum::Router;
use common::*;
use diff_constraints_service::api::{self, AppState};
use diff_constraints_service::model::ConstraintInput;
use diff_constraints_service::store::ConstraintStore;
use serde_json::{json, Value};

fn test_app() -> Router {
    api::app(AppState::new(ConstraintStore::new()))
}

fn batch_body(cs: &[ConstraintInput]) -> Value {
    json!({ "constraints": cs })
}

fn category(json: &Value) -> &str {
    json["error"]["category"]
        .as_str()
        .expect("error.category present")
}

#[tokio::test]
async fn invalid_batch_leaves_set_untouched() {
    let log = RunLog::new("invalid_batch_leaves_set_untouched");
    let app = test_app();

    // 一条合法 + 一条标识符不合法：整批必须被拒，不得留下半更新集合。
    let body = json!({ "constraints": [
        { "name": "ok1", "x": "a", "y": "b", "c": 1 },
        { "name": "bad name!", "x": "a", "y": "b", "c": 1 }
    ]});
    let (status, _, body_json) = post_json(&app, "/v1/constraints:batch", &body).await;
    log.state("reject", (status.as_u16(), category(&body_json)));
    assert_eq!(status, 400);
    assert_eq!(category(&body_json), "input_error");

    let (_, _, view) = get_json(&app, "/v1/constraints").await;
    log.state("snapshot", &view);
    assert_eq!(view["version"], 0, "failed batch must not bump version");
    assert!(
        view["constraints"].as_array().unwrap().is_empty(),
        "no partial set"
    );
    log.verdict(
        "input_error",
        "whole batch rejected; no half-updated set readable",
    );
}

#[tokio::test]
async fn duplicate_name_is_state_conflict_not_input_error() {
    let log = RunLog::new("duplicate_name_is_state_conflict_not_input_error");
    let app = test_app();

    let first = batch_body(&[c("n1", "p", "q", -1)]);
    let (status, _, resp) = post_json(&app, "/v1/constraints:batch", &first).await;
    assert_eq!(status, 200);
    assert_eq!(resp["version"], 1);

    let second = batch_body(&[c("n1", "q", "p", -1)]);
    let (status, _, err) = post_json(&app, "/v1/constraints:batch", &second).await;
    log.state("reject", (status.as_u16(), category(&err)));
    assert_eq!(status, 409);
    assert_eq!(category(&err), "state_conflict");
    assert!(
        err["error"]["message"]
            .as_str()
            .unwrap()
            .contains("already exists"),
        "{err}"
    );

    let (_, _, view) = get_json(&app, "/v1/constraints").await;
    assert_eq!(view["version"], 1, "version unchanged after conflict");
    assert_eq!(view["constraints"].as_array().unwrap().len(), 1);
    log.verdict(
        "state_conflict",
        "name clash with stored set rejected; version stays 1",
    );
}

#[tokio::test]
async fn within_batch_duplicate_name_is_input_error() {
    let log = RunLog::new("within_batch_duplicate_name_is_input_error");
    let app = test_app();
    let body = batch_body(&[c("dup", "a", "b", 1), c("dup", "b", "a", 1)]);
    let (status, _, err) = post_json(&app, "/v1/constraints:batch", &body).await;
    assert_eq!(status, 400);
    assert_eq!(category(&err), "input_error");
    log.verdict(
        "input_error",
        "self-contradictory batch rejected before touching state",
    );
}

#[tokio::test]
async fn unsat_batch_returns_named_cycle_and_changes_nothing() {
    let log = RunLog::new("unsat_batch_returns_named_cycle_and_changes_nothing");
    let app = test_app();

    // 先存入 a1（冲突环将跨越“旧集合 + 新批次”）。
    let seeded = batch_body(&[c("a1", "p", "q", -1)]);
    let (status, _, _) = post_json(&app, "/v1/constraints:batch", &seeded).await;
    assert_eq!(status, 200);

    let bad = batch_body(&[c("a2", "q", "p", -1)]);
    let (status, _, err) = post_json(&app, "/v1/constraints:batch", &bad).await;
    log.state("status", status.as_u16());
    assert_eq!(status, 409);
    assert_eq!(category(&err), "state_conflict");
    assert_eq!(
        err["error"]["details"]["conflict"],
        "constraint_set_unsatisfiable"
    );

    let ev = &err["error"]["details"]["evidence"];
    let cost = ev["total_cost"].as_i64().unwrap();
    log.state("evidence_cost", cost);
    assert!(
        cost < 0,
        "conflict cycle must be strictly negative, got {cost}"
    );
    assert_eq!(cost, -2, "hand-computed: -1 + -1");

    let names: Vec<&str> = ev["cycle"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e["constraint"].as_str().unwrap())
        .collect();
    log.state("cycle_names", &names);
    assert!(
        names.contains(&"a1") && names.contains(&"a2"),
        "evidence cites original ids: {names:?}"
    );
    assert_eq!(names.len(), 2, "no anonymous super-source edges may appear");

    // 环闭合性（从响应 JSON 直接核）。
    let edges = ev["cycle"].as_array().unwrap();
    for i in 0..edges.len() {
        assert_eq!(
            edges[i]["to"],
            edges[(i + 1) % edges.len()]["from"],
            "closed at {i}"
        );
    }

    // 集合与版本保持在提交 a1 之后的状态。
    let (_, _, view) = get_json(&app, "/v1/constraints").await;
    assert_eq!(view["version"], 1);
    assert_eq!(view["constraints"].as_array().unwrap().len(), 1);

    // 当前可行解必须满足当前集合中的每条约束（独立回代）。
    let (_, _, sol) = get_json(&app, "/v1/solution").await;
    let stored: Vec<ConstraintInput> = serde_json::from_value(view["constraints"].clone()).unwrap();
    let assignment: BTreeMap<String, i64> =
        serde_json::from_value(sol["assignment"].clone()).unwrap();
    check_assignment(&stored, &assignment).expect("served assignment satisfies stored set");
    log.state("served_assignment", &assignment);
    log.verdict(
        "state_conflict",
        "unsat batch rejected with negative named cycle (-2); set remains version 1 and feasible",
    );
}

#[tokio::test]
async fn stateless_solve_gives_exact_assignment_and_conflict() {
    let log = RunLog::new("stateless_solve_gives_exact_assignment_and_conflict");
    let app = test_app();

    // 可行：手算 {a:0,b:-2,c:0}（与内核用例相同的系统，经 HTTP 再证一次）。
    let body = json!({ "constraints": [
        { "name": "k1", "x": "a", "y": "b", "c": 5 },
        { "name": "k2", "x": "b", "y": "c", "c": -2 },
        { "name": "k3", "x": "c", "y": "a", "c": 1 }
    ]});
    let (status, _, sol) = post_json(&app, "/v1/solve", &body).await;
    assert_eq!(status, 200);
    let assignment: BTreeMap<String, i64> =
        serde_json::from_value(sol["assignment"].clone()).unwrap();
    log.state("feasible_assignment", &assignment);
    assert_eq!(assignment["a"], 0);
    assert_eq!(assignment["b"], -2);
    assert_eq!(assignment["c"], 0);
    let stored: Vec<ConstraintInput> = serde_json::from_value(body["constraints"].clone()).unwrap();
    check_assignment(&stored, &assignment).expect("independent substitution");

    // 不可行：409 + 严格负环。
    let unsat = json!({ "constraints": [
        { "name": "n1", "x": "p", "y": "q", "c": -1 },
        { "name": "n2", "x": "q", "y": "p", "c": -1 }
    ]});
    let (status, _, err) = post_json(&app, "/v1/solve", &unsat).await;
    assert_eq!(status, 409);
    assert_eq!(
        err["error"]["details"]["evidence"]["total_cost"].as_i64(),
        Some(-2)
    );

    // 无状态端点不得修改服务端集合。
    let (_, _, view) = get_json(&app, "/v1/constraints").await;
    assert_eq!(
        view["version"], 0,
        "stateless solve must not mutate stored set"
    );
    log.verdict(
        "ok",
        "exact feasible assignment served; unsat -> 409 evidence; store untouched",
    );
}

#[tokio::test]
async fn overflow_is_422_computation_failure() {
    let log = RunLog::new("overflow_is_422_computation_failure");
    let app = test_app();
    let body = json!({ "constraints": [
        { "name": "o1", "x": "a", "y": "b", "c": -1 },
        { "name": "o2", "x": "x", "y": "a", "c": i64::MIN }
    ]});
    let (status, _, err) = post_json(&app, "/v1/solve", &body).await;
    log.state("reject", (status.as_u16(), category(&err)));
    assert_eq!(status, 422);
    assert_eq!(category(&err), "computation_failure");
    assert!(err["error"]["message"]
        .as_str()
        .unwrap()
        .contains("overflow"));
    log.verdict(
        "computation_failure",
        "i64 underflow rejected as 422, never wrapped or panicked",
    );
}

#[tokio::test]
async fn malformed_body_and_bad_content_type_are_400() {
    let log = RunLog::new("malformed_body_and_bad_content_type_are_400");
    let app = test_app();

    let (status, _, err) = post_raw(
        &app,
        "/v1/solve",
        Some("application/json"),
        b"{nope".to_vec(),
    )
    .await;
    assert_eq!(status, 400);
    assert_eq!(category(&err), "input_error");

    let (status, _, err) = post_raw(&app, "/v1/solve", Some("text/plain"), b"{}".to_vec()).await;
    assert_eq!(status, 400);
    assert_eq!(category(&err), "input_error");
    log.verdict(
        "input_error",
        "malformed JSON and non-JSON content type both classified 400",
    );
}

#[tokio::test]
async fn quota_is_507_resource_exhausted() {
    let log = RunLog::new("quota_is_507_resource_exhausted");
    let app = test_app();
    let constraints: Vec<Value> = (0..128)
        .map(|i| json!({ "name": format!("v{i}"), "x": format!("v{i}"), "y": format!("v{}", i + 1), "c": 0 }))
        .collect();
    let (status, _, err) =
        post_json(&app, "/v1/solve", &json!({ "constraints": constraints })).await;
    log.state("reject", (status.as_u16(), category(&err)));
    assert_eq!(status, 507);
    assert_eq!(category(&err), "resource_exhausted");
    assert!(err["error"]["message"].as_str().unwrap().contains("129"));
    log.verdict(
        "resource_exhausted",
        "129 variables over 128 limit classified 507",
    );
}

#[tokio::test]
async fn delete_works_and_missing_name_is_404() {
    let log = RunLog::new("delete_works_and_missing_name_is_404");
    let app = test_app();
    let body = batch_body(&[c("d1", "a", "b", 1), c("d2", "b", "c", 2)]);
    let (_, _, _) = post_json(&app, "/v1/constraints:batch", &body).await;

    let (status, _, resp) = delete(&app, "/v1/constraints/d1").await;
    assert_eq!(status, 200);
    assert_eq!(resp["removed"], "d1");
    assert_eq!(resp["version"], 2);

    let (status, _, err) = delete(&app, "/v1/constraints/d1").await;
    assert_eq!(status, 404);
    assert_eq!(category(&err), "not_found");

    let (_, _, view) = get_json(&app, "/v1/constraints").await;
    assert_eq!(view["constraints"].as_array().unwrap().len(), 1);
    log.verdict("ok", "delete bumps version; repeat delete is 404 not_found");
}

#[tokio::test]
async fn verify_endpoint_valid_invalid_and_unknown() {
    let log = RunLog::new("verify_endpoint_valid_invalid_and_unknown");
    let app = test_app();

    // 自带一次性约束集：严格负环 -> valid。
    let body = json!({
        "cycle": ["n1", "n2"],
        "constraints": [
            { "name": "n1", "x": "p", "y": "q", "c": -1 },
            { "name": "n2", "x": "q", "y": "p", "c": -1 }
        ]
    });
    let (status, _, v) = post_json(&app, "/v1/evidence/verify", &body).await;
    assert_eq!(status, 200);
    assert_eq!(v["valid"], true);
    assert_eq!(v["evidence"]["total_cost"], -2);

    // 零权环 -> 200 valid=false 且有理由。
    let zero = json!({
        "cycle": ["z1", "z2"],
        "constraints": [
            { "name": "z1", "x": "x", "y": "y", "c": 0 },
            { "name": "z2", "x": "y", "y": "x", "c": 0 }
        ]
    });
    let (_, _, v) = post_json(&app, "/v1/evidence/verify", &zero).await;
    assert_eq!(v["valid"], false);
    assert!(v["reason"]
        .as_str()
        .unwrap()
        .contains("not strictly negative"));

    // 针对当前（空）集合引用未知 ID -> 409 state_conflict。
    let against_store = json!({ "cycle": ["n1", "n2"] });
    let (status, _, err) = post_json(&app, "/v1/evidence/verify", &against_store).await;
    assert_eq!(status, 409);
    assert_eq!(category(&err), "state_conflict");
    log.verdict(
        "ok",
        "verify distinguishes valid/invalid (200) from missing state (409)",
    );
}

#[tokio::test]
async fn run_id_is_echoed_and_reused_for_replay() {
    let log = RunLog::new("run_id_is_echoed_and_reused_for_replay");
    let app = test_app();
    let body = json!({ "constraints": [
        { "name": "n1", "x": "p", "y": "q", "c": -1 },
        { "name": "n2", "x": "q", "y": "p", "c": -1 }
    ]});
    let (status, headers, err) =
        post_json_with_run(&app, "/v1/constraints:batch", &body, "replay-42").await;
    assert_eq!(status, 409);
    assert_eq!(
        headers.get("x-run-id").and_then(|v| v.to_str().ok()),
        Some("replay-42"),
        "supplied run id must be echoed for replay"
    );
    assert_eq!(err["error"]["run_id"], "replay-42");

    // 不带 run id 时服务端生成形如 r-000001 的编号并回显。
    let (_, headers, _) = post_json(&app, "/v1/solve", &json!({"constraints": []})).await;
    let generated = headers
        .get("x-run-id")
        .and_then(|v| v.to_str().ok())
        .unwrap();
    log.state("generated_run_id", generated);
    assert!(
        generated.starts_with("r-"),
        "server generates run ids like r-000001"
    );
    log.verdict(
        "ok",
        "client run ids reused verbatim; server-generated ids present on every response",
    );
}

#[tokio::test]
async fn concurrent_batches_commit_atomically_in_order() {
    let log = RunLog::new("concurrent_batches_commit_atomically_in_order");
    let app = test_app();

    let mut handles = Vec::new();
    for i in 0..8u32 {
        let app = app.clone();
        handles.push(tokio::spawn(async move {
            let body = batch_body(&[c(
                &format!("c{i}"),
                "a",
                "b",
                i as i64 - 4, // 全部形如 a-b<=k，系统始终可行
            )]);
            post_json(&app, "/v1/constraints:batch", &body).await.0
        }));
    }
    let mut statuses: Vec<u16> = Vec::new();
    for h in handles {
        // 所有任务此前已 spawn；顺序 await 不影响它们在 blocking 线程池上并行执行。
        statuses.push(h.await.expect("task panicked").as_u16());
    }
    log.state("statuses", &statuses);
    assert!(
        statuses.iter().all(|s| *s == 200),
        "all disjoint batches succeed: {statuses:?}"
    );

    let (_, _, view) = get_json(&app, "/v1/constraints").await;
    assert_eq!(view["version"], 8, "exactly eight atomic commits");
    assert_eq!(view["constraints"].as_array().unwrap().len(), 8);

    // 已提交集合必须仍可行。
    let (_, _, sol) = get_json(&app, "/v1/solution").await;
    let stored: Vec<ConstraintInput> = serde_json::from_value(view["constraints"].clone()).unwrap();
    let assignment: BTreeMap<String, i64> =
        serde_json::from_value(sol["assignment"].clone()).unwrap();
    check_assignment(&stored, &assignment).expect("concurrent final set feasible");
    log.verdict(
        "ok",
        "8 concurrent batches => version 8, 8 constraints, final set feasible",
    );
}

#[tokio::test]
async fn reset_replaces_and_rejects_unsat_set() {
    let log = RunLog::new("reset_replaces_and_rejects_unsat_set");
    let app = test_app();
    let _ = post_json(
        &app,
        "/v1/constraints:batch",
        &batch_body(&[c("old", "a", "b", 1)]),
    )
    .await;

    // 原子替换为可行集合。
    let ok = json!({ "constraints": [
        { "name": "new1", "x": "p", "y": "q", "c": 3 }
    ]});
    let (status, _, resp) = post_json(&app, "/v1/reset", &ok).await;
    assert_eq!(status, 200);
    assert_eq!(resp["version"], 2);
    assert_eq!(resp["constraint_count"], 1);

    // 试图替换为不可满足集合：拒绝且现状不变。
    let bad = json!({ "constraints": [
        { "name": "u1", "x": "p", "y": "q", "c": -1 },
        { "name": "u2", "x": "q", "y": "p", "c": -1 }
    ]});
    let (status, _, err) = post_json(&app, "/v1/reset", &bad).await;
    assert_eq!(status, 409);
    assert_eq!(err["error"]["details"]["evidence"]["total_cost"], -2);

    let (_, _, view) = get_json(&app, "/v1/constraints").await;
    assert_eq!(view["version"], 2, "failed reset must not bump version");
    assert_eq!(view["constraints"][0]["name"], "new1");
    log.verdict(
        "state_conflict",
        "unsat reset rejected atomically; set stays at version 2",
    );
}

#[tokio::test]
async fn health_and_unknown_route() {
    let log = RunLog::new("health_and_unknown_route");
    let app = test_app();
    let (status, _, health) = get_json(&app, "/health").await;
    assert_eq!(status, 200);
    assert_eq!(health["status"], "ok");

    let (status, _, err) = get_json(&app, "/no/such/route").await;
    assert_eq!(status, 404);
    assert_eq!(category(&err), "not_found");
    assert!(err["error"]["run_id"].is_string());
    log.verdict(
        "ok",
        "health 200; unknown route uses unified 404 body with run id",
    );
}
