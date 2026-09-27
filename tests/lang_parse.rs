//! Parser and validator tests: concrete error cases, not just "it parses".

mod common;

use symex::lang::{program_from_json, program_from_source, ProgramJson};

#[test]
fn parses_and_infers_widths() {
    let p = program_from_source(
        r#"
        param x: u8;
        param y: u16;
        let z: u8 = x + 1;
        assert(z > 0u8 && y < 100u16);
        "#,
    )
    .expect("valid program");
    assert_eq!(p.params.len(), 2);
    assert_eq!(p.body.len(), 2);
}

#[test]
fn rejects_undeclared_variable() {
    let e = program_from_source("param x: u8; assert(y > 0u8);").unwrap_err();
    assert!(e.to_string().contains("undeclared"), "{e}");
}

#[test]
fn rejects_width_mismatch() {
    let e = program_from_source("param x: u8; let y: u8 = x + 1u16;").unwrap_err();
    assert!(e.to_string().contains("mixes"), "{e}");
}

#[test]
fn rejects_out_of_range_literal() {
    let e = program_from_source("param x: u8; let y: u8 = 300u8;").unwrap_err();
    assert!(e.to_string().contains("does not fit"), "{e}");
}

#[test]
fn rejects_ambiguous_literal() {
    let e = program_from_source("assert(1 == 1);").unwrap_err();
    assert!(e.to_string().contains("cannot infer width"), "{e}");
}

#[test]
fn rejects_duplicate_param() {
    let e = program_from_source("param x: u8; param x: u8;").unwrap_err();
    assert!(e.to_string().contains("duplicate"), "{e}");
}

#[test]
fn rejects_non_boolean_condition() {
    let e = program_from_source("param x: u8; if (x) { let a: u8 = 1u8; }").unwrap_err();
    assert!(e.to_string().contains("boolean"), "{e}");
}

#[test]
fn accepts_hex_and_suffix_forms() {
    program_from_source("param x: u8; let y: u8 = 0xffu8; assert(y == 255u8);").expect("hex");
    program_from_source("param x: u8; let y: u8 = 0x10; assert(y == 16u8);").expect("hex infer");
}

#[test]
fn json_envelope_equivalent_to_source() {
    let j = ProgramJson {
        params: vec!["x: u8".into()],
        body: vec!["let y: u8 = x + 1u8;".into(), "assert(y != 0u8);".into()],
    };
    let a = program_from_json(&j).expect("json program");
    let b = program_from_source("param x: u8; let y: u8 = x + 1u8; assert(y != 0u8);").unwrap();
    assert_eq!(a.params.len(), b.params.len());
    assert_eq!(a.body.len(), b.body.len());
}

#[test]
fn unary_and_bitwise_ops_parse() {
    program_from_source(
        r#"
        param x: u8;
        let a: u8 = -x;
        let b: u8 = ~x;
        let c: u8 = (a | b) & 15u8;
        let d: u8 = c ^ 255u8;
        assert(!(d > 200u8) || x == 0u8);
        "#,
    )
    .expect("unary/bitwise program");
}
