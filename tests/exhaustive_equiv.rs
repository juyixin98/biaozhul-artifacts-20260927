//! Small-domain exhaustive equivalence.
//!
//! For each program, the *expected* set of failing inputs is computed by
//! enumerating the whole parameter domain with the independent concrete
//! interpreter (not the kernel, not Z3). The kernel's findings are then
//! checked for soundness (every reported counterexample really fails, at the
//! blamed node) and completeness (the union of the findings' native path
//! conditions — evaluated by the independent native evaluator — covers every
//! failing input).

mod common;

use std::collections::BTreeSet;

use common::*;
use symex::evidence::concrete::FailureKind as ConcKind;
use symex::evidence::native::eval_pc_over_domain;
use symex::kernel::report::Verdict;
use symex::lang::program_from_source;

struct Case {
    name: &'static str,
    src: &'static str,
}

const CASES: &[Case] = &[
    Case {
        name: "add_wrap",
        src: r#"
            param x: u8;
            let y: u8 = x + 1u8;
            assert(y != 0u8);
        "#,
    },
    Case {
        name: "mul_shrink",
        src: r#"
            param x: u8;
            let y: u8 = x * 3u8;
            assert(y >= x);
        "#,
    },
    Case {
        name: "div_zero",
        src: r#"
            param a: u8;
            param b: u8;
            let c: u8 = a / b;
            assert(c <= a);
        "#,
    },
    Case {
        name: "two_param_add",
        src: r#"
            param a: u8;
            param b: u8;
            let s: u8 = a + b;
            assert(s >= a);
        "#,
    },
    Case {
        name: "branch_abs",
        src: r#"
            param x: u8;
            let mut_ok: u8 = 0u8;
            if (x > 100u8) {
                assert(x > 50u8);
            } else {
                assert(x <= 200u8);
            }
        "#,
    },
    Case {
        name: "sub_underflow",
        src: r#"
            param x: u8;
            let y: u8 = x - 5u8;
            assert(y <= x);
        "#,
    },
    Case {
        // SMT-LIB shift semantics: shifting by the width yields 0, so
        // y == x holds only for x == 0.
        name: "shift_beyond_width",
        src: r#"
            param x: u8;
            let y: u8 = x << 8u8;
            assert(y == x);
        "#,
    },
];

fn input_key(input: &symex::evidence::concrete::ConcreteInput) -> BTreeSet<(String, u64)> {
    input.iter().map(|(k, v)| (k.clone(), *v)).collect()
}

#[test]
fn kernel_findings_match_exhaustive_concrete_oracle() {
    for case in CASES {
        let program = program_from_source(case.src).expect("parse");
        let expected = exhaustive_failures(&program);
        let report = analyze_src(case.src);

        // 1. Verdict agreement with the oracle.
        let expect_unsafe = !expected.is_empty();
        assert_eq!(
            report.verdict == Verdict::Unsafe,
            expect_unsafe,
            "case {}: verdict disagrees with exhaustive oracle",
            case.name
        );

        // 2. Soundness: every reported counterexample really fails, at the
        //    blamed node, with the blamed failure kind.
        let expected_keys: BTreeSet<_> = expected.iter().map(|(i, _)| input_key(i)).collect();
        for f in &report.findings {
            let key: BTreeSet<_> = f
                .counterexample
                .iter()
                .map(|(k, v)| (k.clone(), *v))
                .collect();
            assert!(
                expected_keys.contains(&key),
                "case {}: counterexample {:?} is not a failing input",
                case.name,
                f.counterexample
            );
            let (_, site) = expected
                .iter()
                .find(|(i, _)| input_key(i) == key)
                .expect("checked above");
            assert_eq!(site.node_id, f.node_id, "case {}: failure node", case.name);
            let kind: ConcKind = f.kind.into();
            assert_eq!(site.kind, kind, "case {}: failure kind", case.name);
            assert!(f.replay.reproduced(), "case {}: replay", case.name);
        }

        // 3. Completeness via the native path condition: the union of
        //    inputs satisfying any finding's PC (computed by the independent
        //    native evaluator) must equal the oracle's failing set, and each
        //    PC must be individually sound.
        let params: Vec<(String, u32)> = program
            .params
            .iter()
            .map(|p| (p.name.clone(), p.ty.bits()))
            .collect();
        let mut covered: BTreeSet<BTreeSet<(String, u64)>> = BTreeSet::new();
        for f in &report.findings {
            let sat = eval_pc_over_domain(
                &f.native_path_condition,
                &f.native_ssa,
                &params,
                1 << 20,
            )
            .expect("native PC evaluates");
            for input in &sat {
                let key = input_key(input);
                assert!(
                    expected_keys.contains(&key),
                    "case {}: native PC admits non-failing input {:?}",
                    case.name,
                    input
                );
                covered.insert(key);
            }
        }
        assert_eq!(
            covered, expected_keys,
            "case {}: findings do not cover the exhaustive failing set",
            case.name
        );
    }
}

#[test]
fn safe_programs_have_empty_oracle_and_no_findings() {
    // A program the oracle proves safe on the whole domain must be reported
    // safe with zero findings.
    let src = r#"
        param x: u8;
        let y: u8 = x & 7u8;
        assert(y < 8u8);
    "#;
    let program = program_from_source(src).expect("parse");
    assert!(exhaustive_failures(&program).is_empty());
    let report = analyze_src(src);
    assert_eq!(report.verdict, Verdict::Safe);
    assert!(report.findings.is_empty());
}
