//! Builder/parser rejection tests: each malformed spec must map to a
//! specific [`BuildErrorKind`], not a generic failure.

#![cfg(test)]

use fsm_lang::eval::build_system;
use fsm_lang::parser::parse_system;
use fsm_lang::BuildErrorKind;

fn kind_of(src: &str) -> BuildErrorKind {
    match parse_system(src).and_then(build_system) {
        Err(e) => e.kind,
        Ok(_) => panic!("expected a build error"),
    }
}

#[test]
fn empty_integer_range_is_invalid_domain() {
    assert_eq!(
        kind_of("system e { var { x: int[2..1] } init { x := 2 } transition t { guard: false; then: x := 2 } }"),
        BuildErrorKind::InvalidDomain
    );
}

#[test]
fn unknown_reference_is_classified() {
    assert_eq!(
        kind_of("system e { var { x: bool } init { x := true } transition t { guard: ghost; then: x := false } }"),
        BuildErrorKind::UnknownName
    );
}

#[test]
fn non_boolean_guard_is_type_mismatch() {
    assert_eq!(
        kind_of("system e { var { n: int[0..2] } init { n := 0 } transition t { guard: n + 1; then: n := 0 } }"),
        BuildErrorKind::TypeMismatch
    );
}

#[test]
fn comparing_bool_to_int_is_type_mismatch() {
    assert_eq!(
        kind_of("system e { var { n: int[0..2] } init { n := 0 } transition t { guard: n == true; then: n := 0 } }"),
        BuildErrorKind::TypeMismatch
    );
}

#[test]
fn assigning_same_target_twice_is_rejected() {
    assert_eq!(
        kind_of("system e { var { x: bool } init { x := true } transition t { guard: true; then: x := false, x := true } }"),
        BuildErrorKind::DuplicateAssignment
    );
}

#[test]
fn missing_initial_variable_is_rejected() {
    assert_eq!(
        kind_of("system e { var { x: bool; y: bool } init { x := true } transition t { guard: true; then: x := false } }"),
        BuildErrorKind::MissingVariable
    );
}

#[test]
fn duplicate_variable_is_rejected() {
    assert_eq!(
        kind_of("system e { var { x: bool; x: bool } init { x := true } transition t { guard: true; then: x := false } }"),
        BuildErrorKind::DuplicateName
    );
}

#[test]
fn ambiguous_enum_variant_is_rejected() {
    let src = "system e { var { a: enum { same }; b: enum { same } } \
               init { a := same, b := same } transition t { guard: true; then: a := same } }";
    assert_eq!(kind_of(src), BuildErrorKind::AmbiguousEnumVariant);
}

#[test]
fn syntax_error_reports_position() {
    let e = parse_system("system e { var { x bool } }").unwrap_err();
    assert_eq!(e.kind, BuildErrorKind::Parse);
    assert!(e.position.is_some());
}

#[test]
fn json_spec_builds_and_enum_constant_resolves() {
    let doc = serde_json::json!({
        "name": "e",
        "variables": [
            {"name": "c", "type": "enum", "variants": ["free", "taken"]}
        ],
        "init": {"state": {"c": "free"}},
        "transitions": [
            {"name": "take", "guard": "c == free", "assign": [{"target": "c", "value": "taken"}]}
        ]
    });
    let raw = fsm_lang::json::system_from_json(&doc).unwrap();
    let sys = build_system(raw).unwrap();
    assert_eq!(sys.vars.len(), 1);
    let init = sys.concrete_init.as_ref().expect("concrete init recorded");
    assert_eq!(init[0], fsm_lang::Value::Int(0));
}
