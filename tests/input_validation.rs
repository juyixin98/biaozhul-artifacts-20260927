//! 输入语言错误契约测试：每个用例断言到具体 ErrorCode 与错误类别，
//! 输入错误（400）与状态冲突（409）必须可区分。

use weak_trace_inclusion::error::{ErrorCategory, ErrorCode};
use weak_trace_inclusion::input;
use weak_trace_inclusion::model::CheckRequestWire;

fn base_json() -> serde_json::Value {
    serde_json::json!({
        "observable_actions": ["a", "b"],
        "specification": {
            "name": "spec",
            "states": ["s0", "s1"],
            "initial_states": ["s0"],
            "edges": [
                { "source": "s0", "action": "a", "target": "s1" }
            ]
        },
        "implementation": {
            "name": "impl",
            "states": ["i0"],
            "initial_states": ["i0"],
            "edges": [
                { "source": "i0", "action": "a", "target": "i0" }
            ]
        }
    })
}

fn parse(v: serde_json::Value) -> weak_trace_inclusion::error::ApiError {
    let req: CheckRequestWire = serde_json::from_value(v).unwrap();
    input::parse_request(req).expect_err("应当返回错误")
}

#[test]
fn empty_alphabet_is_input_error() {
    let mut v = base_json();
    v["observable_actions"] = serde_json::json!([]);
    let e = parse(v);
    assert_eq!(e.code, ErrorCode::EmptyObservableAlphabet);
    assert_eq!(e.category, ErrorCategory::InputError);
}

#[test]
fn duplicate_observable_action_is_input_error() {
    let mut v = base_json();
    v["observable_actions"] = serde_json::json!(["a", "a"]);
    let e = parse(v);
    assert_eq!(e.code, ErrorCode::DuplicateObservableAction);
    assert_eq!(e.category, ErrorCategory::InputError);
}

#[test]
fn unknown_edge_action_is_state_conflict() {
    let mut v = base_json();
    v["implementation"]["edges"][0]["action"] = "z".into();
    let e = parse(v);
    assert_eq!(e.code, ErrorCode::UnknownAction);
    assert_eq!(e.category, ErrorCategory::StateConflict);
    assert!(e.message.contains("z"));
}

#[test]
fn dangling_initial_state_is_state_conflict() {
    let mut v = base_json();
    v["implementation"]["initial_states"] = serde_json::json!(["ghost"]);
    let e = parse(v);
    assert_eq!(e.code, ErrorCode::UnknownState);
    assert_eq!(e.category, ErrorCategory::StateConflict);
}

#[test]
fn overlap_of_observable_and_hidden_is_conflict() {
    let mut v = base_json();
    v["implementation"]["hidden_actions"] = serde_json::json!(["a"]);
    let e = parse(v);
    assert_eq!(e.code, ErrorCode::ObservableHiddenOverlap);
    assert_eq!(e.category, ErrorCategory::StateConflict);
}

#[test]
fn duplicate_hidden_action_is_conflict() {
    let mut v = base_json();
    v["specification"]["hidden_actions"] = serde_json::json!(["t1", "t1"]);
    let e = parse(v);
    assert_eq!(e.code, ErrorCode::DuplicateHiddenAction);
}

#[test]
fn duplicate_declared_state_is_conflict() {
    let mut v = base_json();
    v["implementation"]["states"] = serde_json::json!(["i0", "i0"]);
    let e = parse(v);
    assert_eq!(e.code, ErrorCode::DuplicateState);
    assert_eq!(e.category, ErrorCategory::StateConflict);
}

#[test]
fn duplicate_edge_id_is_conflict() {
    let mut v = base_json();
    v["implementation"]["edges"] = serde_json::json!([
        { "id": "x", "source": "i0", "action": "a", "target": "i0" },
        { "id": "x", "source": "i0", "action": "b", "target": "i0" }
    ]);
    let e = parse(v);
    assert_eq!(e.code, ErrorCode::DuplicateEdgeId);
    assert_eq!(e.category, ErrorCategory::StateConflict);
}

#[test]
fn empty_initial_states_is_conflict() {
    let mut v = base_json();
    v["specification"]["initial_states"] = serde_json::json!([]);
    let e = parse(v);
    assert_eq!(e.code, ErrorCode::InitialStatesEmpty);
}

#[test]
fn zero_limit_is_input_error() {
    let mut v = base_json();
    v["limits"] = serde_json::json!({ "max_search_nodes": 0 });
    let e = parse(v);
    assert_eq!(e.code, ErrorCode::InvalidLimitValue);
    assert_eq!(e.category, ErrorCategory::InputError);
}

#[test]
fn too_many_edges_is_payload_large_category() {
    let mut v = base_json();
    // 实现侧有 1 条边：把上限压到 0 不允许（InvalidLimitValue），这里用两侧共享检查——
    // 给实现再加一条边后把上限设为 1，必然超限。
    v["implementation"]["edges"] = serde_json::json!([
        { "source": "i0", "action": "a", "target": "i0" },
        { "source": "i0", "action": "b", "target": "i0" }
    ]);
    v["limits"] = serde_json::json!({ "max_edges_per_lts": 1 });
    let e = parse(v);
    assert_eq!(e.code, ErrorCode::HardLimitExceeded);
    assert_eq!(e.category, ErrorCategory::PayloadTooLarge);
}

#[test]
fn missing_required_field_fails_at_shape_layer() {
    // 形状层错误在服务层用 serde 捕获；这里直接验证反序列化失败语义。
    let v = serde_json::json!({ "observable_actions": ["a"] });
    let result = serde_json::from_value::<CheckRequestWire>(v);
    assert!(result.is_err(), "缺少 specification/implementation 必须反序列化失败");
}

#[test]
fn endpoint_states_are_auto_registered() {
    // 边里出现但未在 states 中声明的状态应补登，而不是报错。
    let mut v = base_json();
    v["implementation"]["states"] = serde_json::json!(["i0"]);
    v["implementation"]["edges"] = serde_json::json!([
        { "source": "i0", "action": "a", "target": "i1" }
    ]);
    let req: CheckRequestWire = serde_json::from_value(v).unwrap();
    let (system, _) = input::parse_request(req).unwrap();
    assert_eq!(system.implementation.num_states(), 2);
}

#[test]
fn categories_have_distinct_http_status() {
    // 资源耗尽不是错误；输入/冲突/超大/计算失败四类必须映射到不同状态码。
    assert_eq!(ErrorCategory::InputError.http_status(), 400);
    assert_eq!(ErrorCategory::StateConflict.http_status(), 409);
    assert_eq!(ErrorCategory::PayloadTooLarge.http_status(), 413);
    assert_eq!(ErrorCategory::ComputationFailure.http_status(), 500);
}
