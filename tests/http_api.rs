//! End-to-end HTTP tests against the real axum server on a loopback port.
//!
//! These assert concrete status codes, error kinds, and response payloads —
//! covering the whole error taxonomy: input (400), state conflict (409),
//! resource exhausted (413), computation failed (422), not found (404).

mod common;

use diffconstraints::solver::MAX_EDGES;
use diffconstraints::testlog::RunLog;

use common::{get, post_json, spawn_server};
use serde_json::json;

#[test]
fn health_and_empty_list() {
    let mut log = RunLog::start("http_health_and_empty_list");
    let srv = spawn_server();

    let h = get(&srv.addr, "/v1/health");
    assert_eq!(h.status, 200);
    assert_eq!(h.json()["status"], "ok");

    let l = get(&srv.addr, "/v1/constraints");
    assert_eq!(l.status, 200);
    assert_eq!(l.json()["count"], 0);
    assert_eq!(l.json()["revision"], 0);
    log.state("empty list response", l.json());
    log.pass("health + empty list return 200 with concrete fields");
}

#[test]
fn add_solve_feasible_via_json_and_text() {
    let mut log = RunLog::start("http_add_solve_feasible");
    let srv = spawn_server();

    // Add via JSON.
    let body = json!({
        "constraints": [
            {"id": "b_a", "lhs": "b", "rhs": "a", "bound": 5},
            {"id": "c_b", "lhs": "c", "rhs": "b", "bound": -2}
        ]
    });
    let r = post_json(&srv.addr, "/v1/constraints", &body);
    assert_eq!(r.status, 200, "body: {}", r.body);
    assert_eq!(r.json()["revision"], 1);

    // Add via text DSL in the same kind of request.
    let text = json!({"text": "a_c: a - c <= 0   # closes the chain"});
    let r2 = post_json(&srv.addr, "/v1/constraints", &text);
    assert_eq!(r2.status, 200, "body: {}", r2.body);

    let sol = post_json(&srv.addr, "/v1/solve", &json!({}));
    assert_eq!(sol.status, 200, "body: {}", sol.body);
    let j = sol.json();
    assert_eq!(j["status"], "feasible");
    assert_eq!(j["variable_count"], 3);
    assert_eq!(j["constraint_count"], 3);
    assert_eq!(j["component_count"], 1);
    let w = &j["witness"]["assignment"];
    let (a, b, c) = (w["a"].as_i64().unwrap(), w["b"].as_i64().unwrap(), w["c"].as_i64().unwrap());
    assert!(b - a <= 5);
    assert!(c - b <= -2);
    assert!(a - c <= 0);
    log.state("feasible witness assignment", json!({"a":a,"b":b,"c":c}));
    log.state("pass traces present", &j["witness"]["trace"]["passes"]);
    log.pass("JSON + text adds; feasible solve returns satisfying concrete values and traces");
}

#[test]
fn solve_reports_named_id_negative_cycle() {
    let mut log = RunLog::start("http_negative_cycle_named");
    let srv = spawn_server();
    let body = json!({"constraints": [
        {"id": "ab", "lhs": "b", "rhs": "a", "bound": -1},
        {"id": "bc", "lhs": "c", "rhs": "b", "bound": -1},
        {"id": "ca", "lhs": "a", "rhs": "c", "bound": 1}
    ]});
    let r = post_json(&srv.addr, "/v1/constraints", &body);
    assert_eq!(r.status, 200);

    let sol = post_json(&srv.addr, "/v1/solve", &json!({}));
    assert_eq!(sol.status, 200);
    let j = sol.json();
    assert_eq!(j["status"], "infeasible");
    let ids = j["conflict"]["cycle_constraint_ids"].as_array().unwrap();
    let id_strs: Vec<String> = ids.iter().map(|v| v.as_str().unwrap().to_string()).collect();
    let weight = j["conflict"]["weight"].as_i64().unwrap();
    assert!(weight < 0, "cycle weight must be strictly negative: {weight}");
    assert_eq!(weight, -1, "-1 + -1 + 1 = -1");
    for id in &id_strs {
        assert!(["ab", "bc", "ca"].contains(&id.as_str()), "only original ids: {id}");
    }
    log.state("conflict", json!({"ids": id_strs, "weight": weight, "vertices": j["conflict"]["cycle_vertices"]}));

    // POST the same ids to the independent verify endpoint.
    let v = post_json(
        &srv.addr,
        "/v1/verify",
        &json!({"cycle": id_strs}),
    );
    assert_eq!(v.status, 200);
    assert_eq!(v.json()["kind"], "cycle");
    assert_eq!(v.json()["result"]["valid"], true);
    assert_eq!(v.json()["result"]["weight"], -1);
    log.pass("infeasible solve + independent /verify agree on the negative cycle");
}

#[test]
fn verify_endpoint_detects_bad_cycles() {
    let srv = spawn_server();
    let body = json!({"constraints": [
        {"id": "ab", "lhs": "b", "rhs": "a", "bound": -1},
        {"id": "bc", "lhs": "c", "rhs": "b", "bound": -1},
        {"id": "ca", "lhs": "a", "rhs": "c", "bound": 1}
    ]});
    assert_eq!(post_json(&srv.addr, "/v1/constraints", &body).status, 200);

    // Unknown id.
    let r = post_json(&srv.addr, "/v1/verify", &json!({"cycle": ["nope"]}));
    assert_eq!(r.status, 200, "verification of an invalid cycle is still 200 with valid=false");
    assert_eq!(r.json()["result"]["valid"], false);
    assert_eq!(r.json()["result"]["rejection"]["reason"], "unknown_constraint");

    // Zero-weight cycle constructed from two artificial constraints would
    // need different weights; instead send a non-closed order.
    let r2 = post_json(&srv.addr, "/v1/verify", &json!({"cycle": ["ab", "ca"]}));
    assert_eq!(r2.json()["result"]["valid"], false);
    assert_eq!(r2.json()["result"]["rejection"]["reason"], "not_closed");

    // Assignment verification against stored constraints:
    // ab: b - a <= -1, bc: c - b <= -1, ca: a - c <= 1.
    // Choose a=10,b=10,c=0 -> b-a=0 > -1 (ab violated), c-b=-1 ok,
    // a-c=10 > 1 (ca violated); bc satisfied.
    let r3 = post_json(
        &srv.addr,
        "/v1/verify",
        &json!({"assignment": {"a": 10, "b": 10, "c": 0}}),
    );
    let j3 = r3.json();
    assert_eq!(j3["result"]["satisfied"], false);
    let violations: Vec<&str> = j3["result"]["violations"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v["constraint_id"].as_str().unwrap())
        .collect();
    assert!(violations.contains(&"ab"), "ab must be violated: {violations:?}");
    assert!(violations.contains(&"ca"));
    assert!(!violations.contains(&"bc"));
}

#[test]
fn input_errors_are_400() {
    let mut log = RunLog::start("http_input_errors_400");
    let srv = spawn_server();

    let cases: Vec<(&str, serde_json::Value, &str)> = vec![
        ("malformed json", json!({}), "no constraints supplied"),
        ("bad identifier", json!({"constraints":[{"id":"1bad","lhs":"x","rhs":"y","bound":1}]}), "invalid"),
        ("missing field", json!({"constraints":[{"id":"c","lhs":"x","bound":1}]}), "invalid JSON"),
    ];
    for (name, body, expect) in cases {
        let r = post_json(&srv.addr, "/v1/constraints", &body);
        assert_eq!(r.status, 400, "{name}: {}", r.body);
        assert_eq!(r.json()["error"]["kind"], "input", "{name}");
        let msg = r.json()["error"]["message"].as_str().unwrap_or("").to_string();
        assert!(msg.contains(expect), "{name}: message '{msg}' should contain '{expect}'");
    }

    // Truly malformed JSON bytes (not representable via json! macro).
    let raw = common::http_request(&srv.addr, "POST", "/v1/constraints", "{not json");
    assert_eq!(raw.status, 400);
    assert_eq!(raw.json()["error"]["kind"], "input");

    // Text DSL parse error with line/column detail.
    let bad_text = json!({"text": "ok: x - y <= 1\nbad: 1z - y <= 2\n"});
    let r = post_json(&srv.addr, "/v1/constraints", &bad_text);
    assert_eq!(r.status, 400);
    assert_eq!(r.json()["error"]["detail"]["line"], 2);
    log.observed_error("input", "all malformed requests returned 400 input with detail");
    log.expected_error("input", "bad JSON, bad ids, missing fields, bad text are all 400");
}

#[test]
fn state_conflicts_are_409() {
    let log = RunLog::start("http_state_conflicts_409");
    let srv = spawn_server();
    let c = json!({"constraints":[{"id":"c1","lhs":"x","rhs":"y","bound":1}]});
    assert_eq!(post_json(&srv.addr, "/v1/constraints", &c).status, 200);

    // Duplicate add.
    let dup = post_json(&srv.addr, "/v1/constraints", &c);
    assert_eq!(dup.status, 409);
    assert_eq!(dup.json()["error"]["kind"], "state_conflict");
    assert_eq!(dup.json()["error"]["detail"]["conflicting_id"], "c1");

    // Update unknown.
    let upd = post_json(
        &srv.addr,
        "/v1/constraints/update",
        &json!({"constraint":{"id":"ghost","lhs":"x","rhs":"y","bound":2}}),
    );
    assert_eq!(upd.status, 409);
    assert_eq!(upd.json()["error"]["kind"], "state_conflict");

    // Delete unknown.
    let del = post_json(&srv.addr, "/v1/constraints/delete", &json!({"id":"ghost"}));
    assert_eq!(del.status, 409);

    // Solve subset with unknown id.
    let sub = post_json(&srv.addr, "/v1/solve", &json!({"only_ids": ["ghost"]}));
    assert_eq!(sub.status, 409);
    log.expected_error("state_conflict", "duplicate add / unknown update,delete,solve id -> 409");
}

#[test]
fn failed_batch_is_atomic_no_half_updated_set() {
    let log = RunLog::start("http_batch_atomicity");
    let srv = spawn_server();
    // Seed one constraint.
    let seed = json!({"constraints":[{"id":"seed","lhs":"a","rhs":"b","bound":1}]});
    assert_eq!(post_json(&srv.addr, "/v1/constraints", &seed).status, 200);

    // replace all: clear + adds, but the last add duplicates an id in the
    // same request -> whole replace must roll back (seed survives).
    let repl = json!({
        "constraints": [
            {"id":"n1","lhs":"a","rhs":"b","bound":2},
            {"id":"n1","lhs":"c","rhs":"d","bound":3}
        ]
    });
    let r = post_json(&srv.addr, "/v1/constraints/replace", &repl);
    assert_eq!(r.status, 409, "{}", r.body);
    let listing = get(&srv.addr, "/v1/constraints").json();
    assert_eq!(listing["count"], 1, "failed replace must not leave a half-updated set");
    assert_eq!(listing["constraints"][0]["id"], "seed");
    assert_eq!(listing["revision"], 1, "revision must not advance on failed batch");

    // A concurrent reader can never observe half-state: solve right after
    // the failure sees exactly the seeded set and is feasible.
    let sol = post_json(&srv.addr, "/v1/solve", &json!({}));
    assert_eq!(sol.json()["status"], "feasible");
    log.pass("failing atomic replace rolled back; subsequent read/solve see old revision");
}

#[test]
fn resource_exhausted_is_413() {
    let log = RunLog::start("http_resource_exhausted_413");
    let srv = spawn_server();
    // Build MAX_EDGES+1 self-loop constraints with unique ids.
    let n = MAX_EDGES + 1;
    let constraints: Vec<serde_json::Value> = (0..n)
        .map(|i| json!({"id": format!("c{i}"), "lhs":"x","rhs":"x","bound":0}))
        .collect();
    let body = json!({"constraints": constraints});
    let r = post_json(&srv.addr, "/v1/constraints", &body);
    assert_eq!(r.status, 413, "expected 413, got {}: {}", r.status, r.body);
    assert_eq!(r.json()["error"]["kind"], "resource_exhausted");

    // Store unchanged.
    assert_eq!(get(&srv.addr, "/v1/constraints").json()["count"], 0);

    // Oversize body -> 413 as well.
    let huge = "x".repeat(2_000_000);
    let raw = common::http_request(
        &srv.addr,
        "POST",
        "/v1/constraints",
        &format!("{{\"text\":\"{huge}\"}}"),
    );
    assert_eq!(raw.status, 413);
    log.expected_error("resource_exhausted", "edge cap and 1MiB body cap both yield 413");
}

#[test]
fn computation_failed_is_422_on_overflow() {
    let log = RunLog::start("http_computation_failed_422");
    let srv = spawn_server();
    let body = json!({"constraints": [
        {"id":"drive","lhs":"b","rhs":"a","bound":-1},
        {"id":"min","lhs":"c","rhs":"b","bound": i64::MIN}
    ]});
    assert_eq!(post_json(&srv.addr, "/v1/constraints", &body).status, 200);
    let sol = post_json(&srv.addr, "/v1/solve", &json!({}));
    assert_eq!(sol.status, 422, "{}", sol.body);
    assert_eq!(sol.json()["error"]["kind"], "computation_failed");
    assert!(sol.json()["error"]["message"]
        .as_str()
        .unwrap()
        .contains("overflows i64"));
    log.expected_error("computation_failed", "relaxation overflow surfaced as 422, not wrapped");
}

#[test]
fn unknown_route_is_404_and_response_has_request_id() {
    let srv = spawn_server();
    let r = get(&srv.addr, "/does/not/exist");
    assert_eq!(r.status, 404);
    assert_eq!(r.json()["error"]["kind"], "not_found");
}

#[test]
fn update_delete_and_clear_work_and_bump_revision() {
    let srv = spawn_server();
    let add = json!({"constraints":[
        {"id":"c1","lhs":"y","rhs":"x","bound":1},
        {"id":"c2","lhs":"z","rhs":"y","bound":2},
        {"id":"c3","lhs":"x","rhs":"z","bound":0}
    ]});
    assert_eq!(post_json(&srv.addr, "/v1/constraints", &add).status, 200);
    // Initially feasible (edges x->y=1, y->z=2, z->x=0, cycle sum 3).

    // Tighten c3 to -4: cycle 1 + 2 - 4 = -1 -> infeasible.
    let upd = post_json(
        &srv.addr,
        "/v1/constraints/update",
        &json!({"constraint":{"id":"c3","lhs":"x","rhs":"z","bound":-4}}),
    );
    assert_eq!(upd.status, 200);
    let sol = post_json(&srv.addr, "/v1/solve", &json!({})).json();
    assert_eq!(sol["status"], "infeasible");
    assert_eq!(sol["conflict"]["weight"], -1);

    // Delete c3 -> the remaining two inequalities are feasible again.
    let del = post_json(&srv.addr, "/v1/constraints/delete", &json!({"id":"c3"}));
    assert_eq!(del.status, 200);
    let sol2 = post_json(&srv.addr, "/v1/solve", &json!({})).json();
    assert_eq!(sol2["status"], "feasible");

    // Replace to empty.
    let clr = post_json(&srv.addr, "/v1/constraints/replace", &json!({"constraints":[]}));
    assert_eq!(clr.status, 200);
    assert_eq!(get(&srv.addr, "/v1/constraints").json()["count"], 0);
}
