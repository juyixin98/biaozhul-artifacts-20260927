//! Input language unit tests: type checking, domain semantics,
//! simultaneous-update semantics, fingerprint stability and compile errors.

use fsm_lang::compile::CompiledSpec;
use fsm_lang::model::*;

fn c_int(v: i64) -> Expr {
    Expr::Const {
        value: Value::Int(v),
    }
}
fn c_bool(v: bool) -> Expr {
    Expr::Const {
        value: Value::Bool(v),
    }
}
fn var(name: &str) -> Expr {
    Expr::Var {
        name: name.to_string(),
    }
}

fn two_int_vars() -> Vec<Variable> {
    vec![
        Variable {
            name: "a".into(),
            domain: Domain::IntRange { lo: -2, hi: 2 },
        },
        Variable {
            name: "b".into(),
            domain: Domain::IntRange { lo: -2, hi: 2 },
        },
    ]
}

fn minimal_spec(vars: Vec<Variable>, initial: Expr, transitions: Vec<Transition>) -> Spec {
    Spec {
        name: "unit".into(),
        variables: vars,
        initial,
        transitions,
        terminal: None,
        properties: vec![],
    }
}

fn identity_transition() -> Vec<Transition> {
    vec![Transition {
        name: "id".into(),
        guard: c_bool(true),
        updates: vec![],
    }]
}

#[test]
fn domain_iter_enumerates_cartesian_product_in_order() {
    let spec = minimal_spec(two_int_vars(), c_bool(false), identity_transition());
    let compiled = CompiledSpec::compile(&spec).unwrap();
    assert_eq!(compiled.total_states(), 25);
    let states: Vec<Vec<Value>> = compiled.iter_domain().collect();
    assert_eq!(states.len(), 25);
    assert_eq!(states[0], vec![Value::Int(-2), Value::Int(-2)]);
    assert_eq!(states[1], vec![Value::Int(-2), Value::Int(-1)]);
    assert_eq!(states[24], vec![Value::Int(2), Value::Int(2)]);

    // Mixed-radix ids are unique (last variable changes fastest, matching
    // the enumeration order: id = a_digit * 5 + b_digit).
    let mut ids: Vec<u128> = states.iter().map(|s| compiled.state_id(s).unwrap()).collect();
    let unique: std::collections::BTreeSet<u128> = ids.iter().copied().collect();
    assert_eq!(unique.len(), 25);
    ids.sort();
    assert_eq!(ids, (0u128..25).collect::<Vec<_>>());
    assert_eq!(compiled.state_id(&states[0]), Some(0));
    assert_eq!(compiled.state_id(&states[1]), Some(1)); // (-2,-1)
    assert_eq!(compiled.state_id(&states[5]), Some(5)); // (-1,-2)
    assert_eq!(compiled.state_id(&states[24]), Some(24)); // (2,2)
}

#[test]
fn bool_domain_has_two_values() {
    let spec = minimal_spec(
        vec![Variable {
            name: "p".into(),
            domain: Domain::Bool,
        }],
        c_bool(false),
        identity_transition(),
    );
    let compiled = CompiledSpec::compile(&spec).unwrap();
    let states: Vec<Vec<Value>> = compiled.iter_domain().collect();
    assert_eq!(states, vec![vec![Value::Bool(false)], vec![Value::Bool(true)]]);
}

#[test]
fn simultaneous_apply_reads_only_pre_state() {
    // swap a,b; both RHS evaluated before any write.
    let spec = minimal_spec(
        two_int_vars(),
        c_bool(false),
        vec![Transition {
            name: "swap".into(),
            guard: c_bool(true),
            updates: vec![
                Update {
                    var: "a".into(),
                    value: var("b"),
                },
                Update {
                    var: "b".into(),
                    value: var("a"),
                },
            ],
        }],
    );
    let compiled = CompiledSpec::compile(&spec).unwrap();
    let t = &compiled.transitions[0];
    let next = compiled
        .apply(t, &[Value::Int(1), Value::Int(-2)])
        .unwrap();
    assert_eq!(next, vec![Value::Int(-2), Value::Int(1)]);
}

#[test]
fn arithmetic_overflow_is_reported_not_wrapped() {
    // A transition whose RHS overflows: compilation succeeds (types are
    // fine), evaluation reports INT_OVERFLOW.
    let mut spec = Spec {
        name: "ovf".into(),
        variables: vec![Variable {
            name: "a".into(),
            domain: Domain::IntRange {
                lo: 0,
                hi: 9223372036854775807,
            },
        }],
        initial: Expr::Eq {
            left: Box::new(var("a")),
            right: Box::new(c_int(9223372036854775807)),
        },
        transitions: vec![Transition {
            name: "double".into(),
            guard: c_bool(true),
            updates: vec![Update {
                var: "a".into(),
                value: Expr::Mul {
                    left: Box::new(var("a")),
                    right: Box::new(c_int(2)),
                },
            }],
        }],
        terminal: None,
        properties: vec![],
    };
    let compiled = CompiledSpec::compile(&spec).unwrap();
    let err = compiled
        .apply(&compiled.transitions[0], &[Value::Int(9223372036854775807)])
        .unwrap_err();
    assert_eq!(err.code, "INT_OVERFLOW");
    let _ = &mut spec;
}

#[test]
fn type_errors_are_rejected_at_compile_time() {
    let mk = |initial: Expr| minimal_spec(two_int_vars(), initial, identity_transition());

    let e = CompiledSpec::compile(&mk(Expr::Add {
        left: Box::new(var("a")),
        right: Box::new(c_bool(true)),
    }))
    .unwrap_err();
    assert_eq!(e.code, "TYPE_MISMATCH");

    let e = CompiledSpec::compile(&mk(var("a"))).unwrap_err();
    assert_eq!(e.code, "TYPE_MISMATCH");

    let e = CompiledSpec::compile(&mk(Expr::And {
        left: Box::new(c_bool(true)),
        right: Box::new(var("a")),
    }))
    .unwrap_err();
    assert_eq!(e.code, "TYPE_MISMATCH");
}

#[test]
fn unknown_and_duplicate_names_are_rejected() {
    let mut spec = minimal_spec(two_int_vars(), c_bool(false), identity_transition());
    spec.transitions[0].updates = vec![Update {
        var: "ghost".into(),
        value: c_int(0),
    }];
    assert_eq!(
        CompiledSpec::compile(&spec).unwrap_err().code,
        "UNKNOWN_VARIABLE"
    );

    let dup = minimal_spec(
        two_int_vars(),
        c_bool(false),
        {
            let mut v = identity_transition();
            v.push(Transition {
                name: "id".into(),
                guard: c_bool(true),
                updates: vec![],
            });
            v
        },
    );
    assert_eq!(
        CompiledSpec::compile(&dup).unwrap_err().code,
        "DUPLICATE_NAME"
    );

    let mut dupv = minimal_spec(
        vec![
            Variable {
                name: "x".into(),
                domain: Domain::Bool,
            },
            Variable {
                name: "x".into(),
                domain: Domain::Bool,
            },
        ],
        c_bool(false),
        identity_transition(),
    );
    dupv.variables[1].domain = Domain::Bool;
    assert_eq!(
        CompiledSpec::compile(&dupv).unwrap_err().code,
        "DUPLICATE_NAME"
    );

    let empty_range = minimal_spec(
        vec![Variable {
            name: "z".into(),
            domain: Domain::IntRange { lo: 5, hi: 2 },
        }],
        c_bool(false),
        identity_transition(),
    );
    assert_eq!(
        CompiledSpec::compile(&empty_range).unwrap_err().code,
        "INVALID_DOMAIN"
    );
}

#[test]
fn duplicate_assignment_in_one_transition_is_rejected() {
    let mut spec = minimal_spec(two_int_vars(), c_bool(false), identity_transition());
    spec.transitions[0].updates = vec![
        Update {
            var: "a".into(),
            value: c_int(0),
        },
        Update {
            var: "a".into(),
            value: c_int(1),
        },
    ];
    assert_eq!(
        CompiledSpec::compile(&spec).unwrap_err().code,
        "DUPLICATE_ASSIGNMENT"
    );
}

#[test]
fn assignment_type_mismatch_is_rejected() {
    let mut spec = minimal_spec(two_int_vars(), c_bool(false), identity_transition());
    spec.transitions[0].updates = vec![Update {
        var: "a".into(),
        value: c_bool(true),
    }];
    assert_eq!(
        CompiledSpec::compile(&spec).unwrap_err().code,
        "TYPE_MISMATCH"
    );
}

#[test]
fn fingerprint_is_key_order_and_whitespace_independent() {
    let path = format!("{}/../../fixtures/counter.json", env!("CARGO_MANIFEST_DIR"));
    let text = std::fs::read_to_string(&path).unwrap();
    let spec: Spec = serde_json::from_str(&text).unwrap();
    let (fp1, canon1) = fsm_lang::fingerprint::fingerprint(&spec).unwrap();

    let value: serde_json::Value = serde_json::from_str(&text).unwrap();
    let reserialized = serde_json::to_string(&value).unwrap();
    let spec2: Spec = serde_json::from_str(&reserialized).unwrap();
    let (fp2, canon2) = fsm_lang::fingerprint::fingerprint(&spec2).unwrap();
    assert_eq!(fp1, fp2);
    assert_eq!(canon1, canon2);
    assert_eq!(fp1.len(), 64, "sha256 hex length");
}
