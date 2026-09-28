//! Solver-backend tests: built-in DPLL vs the independent enumerator on a sweep,
//! UNKNOWN/budget semantics, and the external CLI adapter (only when a shell is
//! available — using a fully local fixture binary, never a network resource).

mod common;

use common::Enumerator;
use mus_core::language::{parse_cnf, Cnf};
use mus_core::solver::{
    builtin::DpllSolver, external::ExternalCliSolver, Budget, CancelToken, SStatus,
    SolveCtx, Solver,
};

fn ctx(budget: u64) -> SolveCtx {
    SolveCtx::new(Budget::new(budget), CancelToken::new())
}

fn sweep_formulas() -> Vec<(String, Cnf)> {
    let texts = [
        ("sat1", "2\na: 1 0\nb: 2 0\n"),
        ("unsat_unit", "1\na: 1 0\nb: -1 0\n"),
        ("unsat_cores", "3\nc1: 1 2 0\nc2: -1 2 0\nc3: 1 -2 0\nc4: -1 -2 0\nr1: 1 3 0\n"),
        ("empty_clause", "1\na: 0\nb: 1 0\n"),
        ("tautology", "1\na: 1 -1 0\n"),
        ("no_clause_body_empty", "0\n"),
        ("three_sat", "3\na: 1 2 3 0\nb: -1 -2 0\nc: 2 -3 0\n"),
        ("single_var_both_pol", "1\na: 1 -1 0\nb: 1 0\n"),
    ];
    texts
        .iter()
        .map(|(n, t)| (n.to_string(), parse_cnf(t).unwrap()))
        .collect()
}

#[test]
fn dpll_agrees_with_independent_enumerator_everywhere() {
    for (name, cnf) in sweep_formulas() {
        let got = DpllSolver::default().solve(&cnf, &ctx(0));
        let expect = Enumerator::decide(&cnf).expect("small instance");
        match expect {
            Some(bits) => {
                assert_eq!(got.status, SStatus::Sat, "{name}: expected SAT");
                let m = got.model.expect("sat must carry a model");
                assert!(cnf.satisfied_by(&m), "{name}: DPLL model must satisfy");
                assert!(
                    common::witness_satisfies(
                        &cnf,
                        &cnf.constraints.iter().map(|c| c.id.clone()).collect(),
                        bits
                    ),
                    "{name}: independent witness check"
                );
            }
            None => assert_eq!(got.status, SStatus::Unsat, "{name}: expected UNSAT"),
        }
    }
}

#[test]
fn budget_zero_limit_refuses_with_unknown_not_unsat() {
    let cnf = parse_cnf("1\na: 1 0\nb: -1 0\n").unwrap();
    let c = SolveCtx::new(Budget::new(0), CancelToken::new());
    // limit 0 = unlimited: first call works.
    assert_eq!(DpllSolver::default().solve(&cnf, &c).status, SStatus::Unsat);

    // A 1-call budget: the second attempted decision must be Unknown.
    let c2 = SolveCtx::new(Budget::new(1), CancelToken::new());
    assert!(c2.budget.tick()); // consume the single credit
    let r = DpllSolver::default().solve(&cnf, &c2);
    assert_eq!(r.status, SStatus::Unknown);
    assert!(r.detail.unwrap().contains("budget"));
}

#[test]
fn cancel_before_solve_yields_unknown() {
    let cnf = parse_cnf("1\na: 1 0\nb: -1 0\n").unwrap();
    let cancel = CancelToken::new();
    cancel.cancel();
    let r = DpllSolver::default().solve(&cnf, &SolveCtx::new(Budget::new(0), cancel));
    assert_eq!(r.status, SStatus::Unknown);
}

#[test]
fn external_adapter_missing_binary_is_unknown_never_unsat() {
    let cnf = parse_cnf("1\na: 1 0\nb: -1 0\n").unwrap();
    let solver = ExternalCliSolver::new("/nonexistent/mus-fake-solver-bin-xyz", vec![]);
    let r = solver.solve(&cnf, &ctx(0));
    assert_eq!(r.status, SStatus::Unknown);
    assert!(r.detail.unwrap().contains("failed to spawn"));
}

/// End-to-end external adapter test against a tiny POSIX-shell "solver" script
/// synthesised locally in a temp dir — no accounts, no downloads.
#[cfg(unix)]
#[test]
fn external_adapter_parses_dimacs_sat_and_unsat() {
    use std::os::unix::fs::PermissionsExt;

    let dir = std::env::temp_dir().join(format!("mus-ext-test-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&dir).unwrap();
    let bin = dir.join("sh-solver.sh");
    // Greps the CNF for the tell-tale UNSAT unit pair `1 0` + `-1 0`; otherwise SAT
    // with the trivial all-positive model (valid for the fixtures we send).
    std::fs::write(
        &bin,
        "#!/bin/sh\n\
         f=\"$1\"\n\
         if grep -Eq '^1 0$' \"$f\" && grep -Eq '^-1 0$' \"$f\"; then\n\
         echo UNSATISFIABLE\n\
         else\n\
         nvars=$(awk '/^p cnf/ {print $3}' \"$f\")\n\
         echo SATISFIABLE\n\
         printf 'v '; i=1; while [ $i -le \"$nvars\" ]; do printf '%d ' $i; i=$((i+1)); done; echo '0'\n\
         fi\n",
    )
    .unwrap();
    std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();

    let solver = ExternalCliSolver::new(bin.to_str().unwrap().to_string(), vec![]);

    let unsat = parse_cnf("1\na: 1 0\nb: -1 0\n").unwrap();
    let r = solver.solve(&unsat, &ctx(0));
    assert_eq!(r.status, SStatus::Unsat);

    let sat = parse_cnf("2\na: 1 2 0\n").unwrap();
    let r = solver.solve(&sat, &ctx(0));
    assert_eq!(r.status, SStatus::Sat, "detail: {:?}", r.detail);
    let model = r.model.unwrap();
    assert!(sat.satisfied_by(&model));

    let _ = std::fs::remove_dir_all(&dir);
}
