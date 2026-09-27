#![allow(dead_code)]
use fsm_core::{run_check, CheckOptions, Query, QueryKind};
use fsm_fixtures::answers::Expected;
use fsm_lang::eval::build_system;
use fsm_lang::parser::{parse_expr, parse_system};
use fsm_lang::System;

/// Build a `System` from a DSL fixture string.
pub fn build(text: &str) -> System {
    let raw = parse_system(text).expect("fixture parses");
    build_system(raw).expect("fixture builds")
}

/// Build queries from (name, kind, expression) triples, resolving them
/// against the system the way the API does.
pub fn run_with(
    sys: &System,
    props: &[(&str, QueryKind, &str)],
    deadlock: bool,
    max_states: u64,
) -> fsm_core::Outcome {
    let mut queries = Vec::new();
    for (name, kind, src) in props {
        let mut e = parse_expr(src).expect("property parses");
        fsm_lang::eval::check_boolean_predicate(sys, &mut e).expect("predicate well-typed");
        queries.push(Query {
            name: (*name).into(),
            kind: *kind,
            predicate: Some(e),
            source: (*src).into(),
        });
    }
    if deadlock {
        queries.push(Query {
            name: "deadlock".into(),
            kind: QueryKind::DeadlockFree,
            predicate: None,
            source: String::new(),
        });
    }
    run_check(sys, &queries, CheckOptions { max_states })
}

pub fn assert_conclusion(out: &fsm_core::Outcome, name: &str, expected: Expected) {
    let p = out
        .properties
        .iter()
        .find(|p| p.name == name)
        .unwrap_or_else(|| panic!("missing property {name}"));
    let got = p.conclusion;
    match expected {
        Expected::Holds => assert_eq!(
            got,
            fsm_core::Conclusion::Holds,
            "property {name}: expected holds, got {got:?}: {:?}",
            p.reason
        ),
        Expected::Violated => assert_eq!(
            got,
            fsm_core::Conclusion::Violated,
            "property {name}: expected violated, got {got:?}"
        ),
        Expected::Unknown => assert_eq!(
            got,
            fsm_core::Conclusion::Unknown,
            "property {name}: expected unknown (truncated), got {got:?}"
        ),
        Expected::RunError(_) => panic!("not a run-error scenario"),
    }
}

pub fn evidence_of<'a>(out: &'a fsm_core::Outcome, name: &str) -> &'a fsm_core::Evidence {
    out.properties
        .iter()
        .find(|p| p.name == name)
        .and_then(|p| p.evidence.as_ref())
        .unwrap_or_else(|| panic!("property {name} should carry evidence"))
}
