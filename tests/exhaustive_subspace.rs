mod common;
use cnf_dpll::evidence::checker::{check_model, check_proof};
use cnf_dpll::evidence::types::Outcome;
use cnf_dpll::normalize::normalize_signed_clauses;
use cnf_dpll::solver::{solve_normalized, Budget};
use common::oracle::{brute_force, BruteResult};

#[test]
fn enumerate_every_subset_of_interesting_clauses() {
    // 12 条覆盖：空、单位、二元、三元、重言、重复、跨 3 变量。
    let pool: Vec<Vec<i64>> = vec![
        vec![], vec![1], vec![-1], vec![2], vec![-2], vec![3],
        vec![1, 2], vec![-1, -2], vec![1, -2, 3], vec![-1, 2],
        vec![1, 1, 2], vec![2, -2, 3],
    ];
    let n = pool.len();
    let mut sat = 0; let mut unsat = 0;
    for mask in 0u64..(1 << n) {
        let mut clauses = Vec::new();
        for (i, c) in pool.iter().enumerate() {
            if mask & (1 << i) != 0 { clauses.push(c.clone()); }
        }
        let oracle = brute_force(3, &clauses);
        let cnf = normalize_signed_clauses(3, &clauses);
        let r = solve_normalized(&cnf, &Budget::unlimited());
        match (&r.outcome, &oracle) {
            (Outcome::Sat{model}, BruteResult::Sat{..}) => {
                check_model(3, &clauses, model)
                    .unwrap_or_else(|e| panic!("mask={mask:b} 模型被拒: {e}"));
                sat += 1;
            }
            (Outcome::Unsat{proof}, BruteResult::Unsat) => {
                check_proof(3, &clauses, proof)
                    .unwrap_or_else(|e| panic!("mask={mask:b} 证明被拒: {e}"));
                unsat += 1;
            }
            (g, w) => panic!("mask={mask:b} solver={g:?} oracle={w:?}"),
        }
    }
    eprintln!("枚举完成: SAT={sat} UNSAT={unsat} 总计={}", sat+unsat);
    assert_eq!(sat + unsat, 1 << n);
}
