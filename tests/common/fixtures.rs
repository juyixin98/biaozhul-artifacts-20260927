//! Hand-built synthetic fixtures. All expected answers are written into the tests as
//! concrete id sets (and cross-checked by the independent oracle); nothing here comes
//! from the implementation under test.

use mus_service::language::{ClauseId, Formula};

use super::clause;

/// Shared-clause overlapping cores:
///
/// ```text
/// core A: a=(1), b=(-1 or 2), c=(-2)        (forces 1, then 2, contradicted by -2)
/// core B: a=(1), d=(-1 or 3), e=(-3)        (shares a; forces 3, contradicted by -3)
/// ```
///
/// `a` participates in BOTH contradictions. Additional constraints:
/// - `r_unit`: unrelated unit (4)            — redundant, deleted
/// - `r_dup_c`: content-duplicate of `c`     — distinct identity, survives in core
/// - `r_taut`: tautology (1 or -1)           — vacuous, deleted
/// - `r_weak`: (1 or 2), subsumed by `a`     — deleted *only while a survives*
///
/// Deletion-based extraction in INPUT order proves `a` removable (B stays UNSAT
/// without it); b and c follow. Once `a` is gone, `r_weak` is no longer subsumed and
/// becomes *necessary* (with -2 it forces x1=true, feeding core B's chain). The
/// resulting subset-minimal core, hand-derived and oracle-confirmed, is therefore:
/// `[d, e, r_dup_c, r_weak]` — not `[a, d, e]`. This is the intended demonstration
/// that deletion picks one valid MUS among several overlapping ones and that clause
/// identity matters: r_dup_c is not merged with c.
pub fn overlapping_with_redundancy() -> Formula {
    Formula {
        clauses: vec![
            clause("a", &[1]),
            clause("b", &[-1, 2]),
            clause("c", &[-2]),
            clause("d", &[-1, 3]),
            clause("e", &[-3]),
            clause("r_unit", &[4]),
            clause("r_dup_c", &[-2]),
            clause("r_taut", &[1, -1]),
            clause("r_weak", &[1, 2]),
        ],
    }
}

/// Reference subset-minimal core for [`overlapping_with_redundancy`] in input order.
pub const OVERLAP_EXPECTED_CORE: &[&str] = &["d", "e", "r_dup_c", "r_weak"];

/// Ids that are redundant in ANY core and must always be deleted.
pub const ALWAYS_DELETED: &[&str] = &["a", "b", "c", "r_unit", "r_taut"];

/// Clause id -> raw DIMACS ints, for independent oracle evaluation.
pub fn clause_ints(f: &Formula, id: &str) -> Vec<i64> {
    let c = f
        .clauses
        .iter()
        .find(|c| c.id == ClauseId::new(id))
        .unwrap_or_else(|| panic!("fixture has no clause {id}"));
    c.literals.iter().map(|l| l.dimacs()).collect()
}

/// Core B in raw ints (independent reference answer).
pub fn core_b_ints() -> Vec<Vec<i64>> {
    vec![vec![1], vec![-1, 3], vec![-3]]
}

/// Core A in raw ints.
pub fn core_a_ints() -> Vec<Vec<i64>> {
    vec![vec![1], vec![-1, 2], vec![-2]]
}

/// Two DISJOINT cores in one formula:
/// ```text
/// A (size 3): a=(1), b=(-1 or 2), c=(-2)
/// B (size 2): d=(3), e=(-3)
/// ```
/// Both are subset-minimal; B is cardinality-smaller. Deletion-based extraction
/// returns exactly one of them depending on clause order — this fixture demonstrates
/// that subset-minimality does NOT mean cardinality-minimality and that the service
/// never claims the latter.
pub fn disjoint_cores() -> Formula {
    Formula {
        clauses: vec![
            clause("a", &[1]),
            clause("b", &[-1, 2]),
            clause("c", &[-2]),
            clause("d", &[3]),
            clause("e", &[-3]),
        ]
    }
}

/// Same five clauses, B listed first.
pub fn disjoint_cores_reversed() -> Formula {
    Formula {
        clauses: vec![
            clause("d", &[3]),
            clause("e", &[-3]),
            clause("a", &[1]),
            clause("b", &[-1, 2]),
            clause("c", &[-2]),
        ]
    }
}

/// Pure contradiction: single empty clause.
pub fn empty_clause() -> Formula {
    Formula {
        clauses: vec![clause("bang", &[])],
    }
}

/// Raw JSON request bodies used by HTTP-level tests.
pub mod json {
    pub const OVERLAP_BODY: &str = r#"{
        "clauses": [
            {"id": "a", "literals": [1]},
            {"id": "b", "literals": [-1, 2]},
            {"id": "c", "literals": [-2]},
            {"id": "d", "literals": [-1, 3]},
            {"id": "e", "literals": [-3]},
            {"id": "r_unit", "literals": [4]},
            {"id": "r_dup_c", "literals": [-2]},
            {"id": "r_taut", "literals": [1, -1]},
            {"id": "r_weak", "literals": [1, 2]}
        ],
        "verify": true
    }"#;

    pub const BUDGET_BODY: &str = r#"{
        "clauses": [
            {"id": "a", "literals": [1]},
            {"id": "b", "literals": [-1, 2]},
            {"id": "c", "literals": [-2]},
            {"id": "d", "literals": [-1, 3]},
            {"id": "e", "literals": [-3]}
        ],
        "max_solver_calls": 3
    }"#;

    pub const SAT_BODY: &str = r#"{
        "clauses": [
            {"id": "s1", "literals": [1]},
            {"id": "s2", "literals": [2]}
        ]
    }"#;

    /// Unique literals 1111/-1112 must never appear in logs or in the response payload
    /// because every clause is marked sensitive.
    pub const SENSITIVE_BODY: &str = r#"{
        "clauses": [
            {"id": "secret-alpha", "literals": [1111], "sensitive": true},
            {"id": "secret-beta", "literals": [-1111], "sensitive": true}
        ],
        "verify": false
    }"#;
}
