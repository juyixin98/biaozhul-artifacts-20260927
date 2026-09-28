//! Exhaustive small-domain soundness tests.
//!
//! For every test program we enumerate the COMPLETE Cartesian input domain
//! (input ranges are intentionally tiny), run the bounded concrete
//! interpreter on each tuple, and assert that:
//!
//! 1. every concrete value in every final scalar / array element is contained
//!    in the abstract exit state;
//! 2. every concrete value observed at an assignment statement is contained in
//!    the corresponding abstract observation (per program point);
//! 3. every concrete run that fails (overflow / OOB / assertion) is matched by
//!    a non-safe abstract check at exactly that source span and failure
//!    category — and symmetrically no abstract Safe check ever fails
//!    concretely.
//!
//! The reference executor (`concrete`) is a separate implementation of the
//! language semantics; it shares no transfer-function code with the interval
//! engine. Hand-written expected summaries are asserted in fixtures_check.rs.

use interval_analyzer::concrete::{run, ConcErrorKind};
use interval_analyzer::kernel::Interval;
use interval_analyzer::lang::parse;
use interval_analyzer::report::{AnalysisReport, CheckKind, Verdict};
use interval_analyzer::{analyze_source, config::Config};
use serde_json::Value;
use std::collections::BTreeMap;

fn enumerate_domain(src: &str) -> Vec<BTreeMap<String, i64>> {
    // Collect declared input ranges straight from the parsed AST.
    let prog = parse(src).expect("parse");
    let mut ranges: Vec<(String, i64, i64)> = Vec::new();
    fn walk(stmts: &[interval_analyzer::lang::Stmt], out: &mut Vec<(String, i64, i64)>) {
        for s in stmts {
            if let interval_analyzer::lang::Stmt::Input { name, lo, hi, .. } = s {
                out.push((name.clone(), *lo, *hi));
            }
        }
    }
    walk(&prog, &mut ranges);

    let mut combos: Vec<BTreeMap<String, i64>> = vec![BTreeMap::new()];
    for (name, lo, hi) in ranges {
        assert!(
            hi - lo <= 40,
            "exhaustive domains must stay tiny: {name} range {lo}..{hi}"
        );
        let mut next = Vec::new();
        for env in &combos {
            for v in lo..=hi {
                let mut e = env.clone();
                e.insert(name.clone(), v);
                next.push(e);
            }
        }
        combos = next;
    }
    combos
}

fn bound_contains(interval: &Value, v: i64) -> bool {
    // interval is the serde representation: "Bottom" or {"R":{"lo":...,"hi":...}}
    // Finite bounds serialise as strings (i128 has no native JSON support).
    if interval == &Value::String("Bottom".into()) {
        return false;
    }
    let r = &interval["R"];
    let x = v as i128;
    let above_lo = match &r["lo"] {
        Value::String(s) if s == "NegInf" => true,
        Value::String(s) if s == "PosInf" => false,
        b => {
            let f: i128 = b["Fin"].as_str().unwrap().parse().unwrap();
            x >= f
        }
    };
    let below_hi = match &r["hi"] {
        Value::String(s) if s == "PosInf" => true,
        Value::String(s) if s == "NegInf" => false,
        b => {
            let f: i128 = b["Fin"].as_str().unwrap().parse().unwrap();
            x <= f
        }
    };
    above_lo && below_hi
}

fn exit_json(report: &AnalysisReport) -> Value {
    serde_json::to_value(&report.exit_state).expect("serialise")
}

fn expected_kind_matches(ck: CheckKind, err: &ConcErrorKind) -> bool {
    matches!(
        (ck, err),
        (CheckKind::Overflow, ConcErrorKind::Overflow)
            | (CheckKind::ArrayIndex, ConcErrorKind::OutOfBounds { .. })
            | (CheckKind::Assert, ConcErrorKind::AssertFailed)
    )
}

struct ProgramCase {
    name: &'static str,
    src: &'static str,
}

fn cases() -> Vec<ProgramCase> {
    vec![
        ProgramCase {
            name: "loop_grow_small",
            src: "\
let n: [0, 4];
array a[4];
i := 0;
while i < n {
  a[i] := i * 2;
  i := i + 1;
}",
        },
        ProgramCase {
            name: "branch_split",
            src: "\
let x: [-3, 3];
if x >= 0 {
  y := x + 1;
} else {
  y := 0 - x;
}",
        },
        ProgramCase {
            name: "partial_overflow_mul",
            // 3 * 3e18 < i64::MAX (9e18), 4 * 3e18 > i64::MAX
            src: "let x: [3, 6]; y := x * 3000000000000000000;",
        },
        ProgramCase {
            name: "definite_overflow_mul",
            src: "let x: [4, 5]; y := x * 3000000000000000000;",
        },
        ProgramCase {
            name: "partial_oob",
            src: "let i: [0, 3]; array a[2]; v := a[i];",
        },
        ProgramCase {
            name: "definite_oob",
            src: "let i: [5, 7]; array a[2]; v := a[i];",
        },
        ProgramCase {
            name: "assert_sometimes_fails",
            src: "let x: [0, 4]; assert x <= 2; y := x + 1;",
        },
        ProgramCase {
            name: "nested_loop_accum",
            src: "\
let n: [0, 3];
let m: [0, 2];
i := 0;
t := 0;
while i < n {
  j := 0;
  while j < m {
    t := t + 1;
    j := j + 1;
  }
  i := i + 1;
}",
        },
        ProgramCase {
            name: "branch_narrows_overflow_away",
            // guarded multiplication: x < 3 on the path that multiplies
            src: "\
let x: [0, 8];
if x < 3 {
  y := x * 3000000000000000000;
} else {
  y := 0;
}",
        },
    ]
}

#[test]
fn exhaustive_concrete_executions_are_contained_in_abstract_results() {
    let cfg = Config::default();
    for case in cases() {
        let prog = parse(case.src).expect(case.name);
        let report = analyze_source(case.src, cfg.clone()).expect(case.name);
        let exit = exit_json(&report);
        let observations: BTreeMap<u32, Interval> = report
            .observations
            .iter()
            .map(|o| match o {
                interval_analyzer::report::Observation::AssignValue { span, interval, .. } => {
                    (span.offset, *interval)
                }
            })
            .collect();
        let array_elem: BTreeMap<String, Interval> = report
            .exit_state
            .arrays
            .iter()
            .map(|(k, a)| (k.clone(), a.elem))
            .collect();

        let combos = enumerate_domain(case.src);
        assert!(!combos.is_empty(), "{}: no inputs enumerated", case.name);

        let mut ok_runs = 0usize;
        let mut err_runs = 0usize;
        for env in &combos {
            let out = run(&prog, env, 200_000);
            if let Some(e) = &out.error {
                err_runs += 1;
                // Property 3: every concrete failure is flagged by a non-safe
                // abstract check of the matching kind at the same span.
                let matched = report.checks.iter().any(|c| {
                    c.span.offset == e.span.offset
                        && expected_kind_matches(c.kind, &e.kind)
                        && matches!(c.verdict, Verdict::MaybeViolated | Verdict::Violated)
                });
                assert!(
                    matched,
                    "{}: concrete failure {:?} at offset {} is not covered by a matching \
                     maybe/violated check (checks: {:?})",
                    case.name,
                    e.kind,
                    e.span.offset,
                    report
                        .checks
                        .iter()
                        .map(|c| (c.kind_label(), c.span.offset, c.verdict.to_string()))
                        .collect::<Vec<_>>()
                );
                continue;
            }
            ok_runs += 1;

            // Property 1a: final scalars contained in abstract exit state
            for (name, v) in &out.final_vars {
                let iv = exit["vars"]
                    .get(name)
                    .unwrap_or_else(|| panic!("{}: exit state missing scalar {name}", case.name));
                assert!(
                    bound_contains(iv, *v),
                    "{}: concrete {name}={v} not contained in abstract {iv}",
                    case.name
                );
            }
            // Property 1b: final array elements contained in element interval
            for (aname, arr) in &out.final_arrays {
                let elem = array_elem
                    .get(aname)
                    .unwrap_or_else(|| panic!("{}: exit state missing array {aname}", case.name));
                for v in arr {
                    assert!(
                        elem.contains_i64(*v),
                        "{}: concrete array {aname} elem {v} not in {elem}",
                        case.name
                    );
                }
            }
            // Property 2: per-assignment observation containment
            for (off, (lo, hi)) in &out.obs.assigns {
                let iv = observations.get(off).unwrap_or_else(|| {
                    panic!("{}: no abstract observation at offset {off}", case.name)
                });
                assert!(
                    iv.contains_i64(*lo),
                    "{}: concrete assign min {lo} not in {iv}",
                    case.name
                );
                assert!(
                    iv.contains_i64(*hi),
                    "{}: concrete assign max {hi} not in {iv}",
                    case.name
                );
            }
            for (aname, (lo, hi)) in &out.obs.array_stored {
                let elem = array_elem.get(aname).unwrap();
                assert!(
                    elem.contains_i64(*lo),
                    "{}: stored min {lo} not in {elem}",
                    case.name
                );
                assert!(
                    elem.contains_i64(*hi),
                    "{}: stored max {hi} not in {elem}",
                    case.name
                );
            }
        }

        // Property 3 (contrapositive): an abstract Safe check must never fail
        // concretely; an abstract Violated check that is reached must always
        // fail concretely.
        for c in &report.checks {
            for env in &combos {
                let out = run(&prog, env, 200_000);
                if let Some(e) = &out.error {
                    if c.verdict == Verdict::Safe {
                        assert_ne!(
                            c.span.offset, e.span.offset,
                            "{}: abstract-safe check at offset {} failed concretely (inputs {env:?})",
                            case.name, c.span.offset
                        );
                    }
                }
            }
        }

        // Every case must exercise at least one of: error paths or value
        // containment, so the test cannot pass vacuously.
        assert!(
            ok_runs + err_runs == combos.len() && !combos.is_empty(),
            "{}: vacuous case",
            case.name
        );
        eprintln!(
            "{}: {} concrete runs ({} ok, {} failure paths), {} abstract checks",
            case.name,
            combos.len(),
            ok_runs,
            err_runs,
            report.checks.len()
        );
    }
}

#[test]
fn definite_violation_cases_have_no_successful_executions_through_the_fault() {
    // For the two "definite" cases, enumerate and assert EVERY concrete run
    // reaches the matching failure — a Violated verdict must be a real
    // necessity, not an over-approximation relabeled as certain.
    let cfg = Config::default();
    type DefiniteCase = (&'static str, &'static str, fn(&ConcErrorKind) -> bool);
    let definite_cases: &[DefiniteCase] = &[
        (
            "definite_overflow_mul",
            "let x: [4, 5]; y := x * 3000000000000000000;",
            |k| matches!(k, ConcErrorKind::Overflow),
        ),
        (
            "definite_oob",
            "let i: [5, 7]; array a[2]; v := a[i];",
            |k| matches!(k, ConcErrorKind::OutOfBounds { .. }),
        ),
    ];
    for (name, src, is_kind) in definite_cases {
        let prog = parse(src).unwrap();
        let report = analyze_source(src, cfg.clone()).unwrap();
        // and the abstract side must also call it definite
        assert!(
            report
                .checks
                .iter()
                .any(|c| c.verdict == Verdict::Violated && is_expected_kind(c.kind, name)),
            "{name}: abstract report did not mark the fault violated"
        );
        for env in enumerate_domain(src) {
            let out = run(&prog, &env, 200_000);
            let e = out
                .error
                .as_ref()
                .unwrap_or_else(|| panic!("{name}: expected failure for {env:?}"));
            assert!(
                is_kind(&e.kind),
                "{name}: wrong failure category {:?} for {env:?}",
                e.kind
            );
        }
    }
}

fn is_expected_kind(ck: CheckKind, name: &str) -> bool {
    match name {
        "definite_overflow_mul" => ck == CheckKind::Overflow,
        "definite_oob" => ck == CheckKind::ArrayIndex,
        _ => false,
    }
}
