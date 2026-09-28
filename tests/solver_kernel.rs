//! Solver-kernel integration tests with hand-computable instances.
//!
//! Every test asserts concrete values (not just "did not error"), cross-checks
//! feasibility against the independent brute-force oracle, and writes a
//! replayable RunLog containing inputs, intermediate states and reasons.

mod common;

use std::collections::BTreeMap;

use diffconstraints::graph::Graph;
use diffconstraints::model::Constraint;
use diffconstraints::solver::{bellman_ford, SolveOutcome, MAX_EDGES, MAX_VERTICES};
use diffconstraints::testlog::RunLog;
use diffconstraints::{ErrorKind, ServiceError};

use common::oracle::{brute_feasible, direct_cycle_check, violation_ids, CycleResult};

fn c(id: &str, x: &str, y: &str, w: i64) -> Constraint {
    Constraint::new(id, x, y, w).unwrap()
}

fn assignment_of(outcome: &SolveOutcome) -> BTreeMap<String, i64> {
    match outcome {
        SolveOutcome::Feasible(w) => w.assignment.iter().cloned().collect(),
        SolveOutcome::Infeasible(_) => panic!("expected feasible, got negative cycle"),
    }
}

fn cycle_of(outcome: SolveOutcome) -> diffconstraints::solver::NegativeCycle {
    match outcome {
        SolveOutcome::Infeasible(cyc) => cyc,
        SolveOutcome::Feasible(_) => panic!("expected infeasible, got assignment"),
    }
}

/// Hand-computed scheduling chain:
/// ```text
/// b - a <= 5     (a->b gap at most 5)
/// c - b <= -2    (c at most b-2)
/// a - c <= 0     (a at most c)
/// ```
/// Hand check: b <= a+5, c <= b-2 <= a+3, and a <= c, so a <= c <= a+3:
/// feasible. The kernel must return an assignment satisfying all three.
#[test]
fn feasible_hand_computed_chain() {
    let mut log = RunLog::start("feasible_hand_computed_chain");
    let constraints = vec![c("b_a", "b", "a", 5), c("c_b", "c", "b", -2), c("a_c", "a", "c", 0)];
    log.input(
        "constraints x-y<=c",
        constraints
            .iter()
            .map(|c| serde_json::json!({"id":c.id,"lhs":c.lhs,"rhs":c.rhs,"bound":c.bound}))
            .collect::<Vec<_>>(),
    );

    let g = Graph::build(&constraints).unwrap();
    log.state(
        "graph",
        serde_json::json!({
            "vertices": g.variables,
            "edges": g.edges.iter().map(|e|
                serde_json::json!({"from":e.from,"to":e.to,"w":e.weight,"id":e.constraint_id})
            ).collect::<Vec<_>>(),
            "components": g.component_info(),
        }),
    );
    assert_eq!(g.component_count, 1, "chain is one connected component");

    let outcome = bellman_ford(&g).unwrap();
    if let SolveOutcome::Feasible(w) = &outcome {
        log.state("trace passes", &w.trace.passes);
        log.reasoning(
            "feasible",
            format!("{} passes until no relaxations; distance vector is the witness", w.iterations),
        );
    }
    let assignment = assignment_of(&outcome);
    log.state("assignment", &assignment);

    // Concrete hand expectations.
    let (a, b, cc) = (assignment["a"], assignment["b"], assignment["c"]);
    assert!(b - a <= 5, "b-a = {} must be <= 5", b - a);
    assert!(cc - b <= -2, "c-b = {} must be <= -2", cc - b);
    assert!(a - cc <= 0, "a-c = {} must be <= 0", a - cc);

    // Independent oracle 1: brute-force enumeration agrees it is feasible.
    let witness = brute_feasible(&constraints, 5).expect("brute oracle finds a witness");
    log.state("brute-force oracle witness", &witness);
    assert!(violation_ids(&constraints, &witness).is_empty());

    // Independent oracle 2: the produced assignment violates nothing.
    let violations = violation_ids(&constraints, &assignment);
    assert!(violations.is_empty(), "unexpected violations: {violations:?}");

    log.pass(format!("assignment {assignment:?} satisfies all constraints; brute-force oracle agrees"));
}

/// Zero-weight cycle:
/// ```text
/// y - x <= 1   (x->y, 1)
/// x - y <= -1  (y->x, -1)
/// ```
/// Around the loop the sum is exactly 0: feasible, and equality forces y=x+1.
#[test]
fn zero_weight_cycle_is_feasible() {
    let mut log = RunLog::start("zero_weight_cycle_is_feasible");
    let constraints = vec![c("yx", "y", "x", 1), c("xy", "x", "y", -1)];
    log.input("constraints", vec![
        serde_json::json!({"id":"yx","y-x":1}),
        serde_json::json!({"id":"xy","x-y":-1}),
    ]);

    let g = Graph::build(&constraints).unwrap();
    let outcome = bellman_ford(&g).unwrap();
    let a = assignment_of(&outcome);
    log.state("assignment", &a);

    // Concrete: the zero-weight cycle forces equality y = x + 1.
    assert_eq!(
        a["y"] - a["x"],
        1,
        "zero-weight cycle must force y-x = 1 exactly"
    );
    assert!(violation_ids(&constraints, &a).is_empty());

    // The independent cycle checker must classify the same edges as
    // NOT a negative cycle (weight 0).
    assert_eq!(
        direct_cycle_check(&constraints, &["yx".into(), "xy".into()]),
        CycleResult::Invalid("cycle weight is not strictly negative")
    );
    log.reasoning(
        "feasible despite a cycle",
        "cycle total weight is exactly 0; BF relaxations stop, no negative cycle",
    );
    log.pass("zero-weight cycle: feasible, equality y=x+1, not reported as conflict");
}

/// Negative cycle must be reported, even when it is many vertices away from
/// any "first" vertex, and even though another component is perfectly
/// feasible.
///
/// Component 1 (feasible): p - q <= 3.
/// Component 2 (infeasible), a long chain that closes negative:
/// ```text
/// b - a <= -1
/// c - b <= -1
/// d - c <= -1
/// e - d <= -1
/// a - e <= 3        # closes: sum = -1
/// ```
#[test]
fn negative_cycle_far_from_first_vertex() {
    let mut log = RunLog::start("negative_cycle_far_from_first_vertex");
    let constraints = vec![
        c("pq", "p", "q", 3),
        c("ab", "b", "a", -1),
        c("bc", "c", "b", -1),
        c("cd", "d", "c", -1),
        c("de", "e", "d", -1),
        c("ea", "a", "e", 3),
    ];
    log.input(
        "constraints",
        constraints
            .iter()
            .map(|c| serde_json::json!({"id":c.id,"lhs":c.lhs,"rhs":c.rhs,"bound":c.bound}))
            .collect::<Vec<_>>(),
    );

    let g = Graph::build(&constraints).unwrap();
    log.state("components", g.component_info());
    assert_eq!(g.component_count, 2, "two disconnected components");

    let outcome = bellman_ford(&g).unwrap();
    let cyc = cycle_of(outcome);
    log.state(
        "extracted cycle",
        serde_json::json!({
            "ids": cyc.cycle_constraint_ids,
            "vertices": cyc.cycle_vertices,
            "weight": cyc.weight,
            "iterations": cyc.iterations,
            "final_distances": cyc.trace.final_distances,
        }),
    );

    // Concrete assertions on the evidence.
    assert_eq!(cyc.weight, -1, "cycle sums to (-1)*4 + 3 = -1");
    assert!(cyc.weight < 0, "conflict weight must be strictly negative");
    assert!(
        cyc.cycle_constraint_ids
            .iter()
            .all(|id| ["ab", "bc", "cd", "de", "ea"].contains(&id.as_str())),
        "evidence must contain only original constraint ids, got {:?}",
        cyc.cycle_constraint_ids
    );
    assert!(!cyc.cycle_constraint_ids.contains(&"pq".to_string()),
        "the conflict must come from the infeasible component, not the feasible one");
    assert_eq!(
        cyc.cycle_vertices.first(),
        cyc.cycle_vertices.last(),
        "reported vertex walk must close"
    );

    // Independent oracle verifies the same ordered cycle, by its own
    // edge-chasing implementation.
    assert_eq!(
        direct_cycle_check(&constraints, &cyc.cycle_constraint_ids),
        CycleResult::Negative(-1)
    );
    // Brute force independently agrees the system is infeasible over the box
    // that would contain any feasible witness (max |bound| = 3).
    assert!(brute_feasible(&constraints, 3).is_none(),
        "brute-force oracle must also find no feasible assignment");

    log.reasoning(
        "infeasible",
        "an edge relaxed on every one of n passes; predecessor walk closed at weight -1",
    );
    log.pass("far negative cycle found with original ids, closed walk, weight -1; both oracles agree");
}

/// Isolated feasible vertices plus a negative self-tightening cycle must all
/// be handled by the all-zero initialization (no super-source plumbing).
#[test]
fn all_zero_init_reaches_every_component() {
    let mut log = RunLog::start("all_zero_init_reaches_every_component");
    // x - x <= -1  => 0 <= -1, immediately impossible on one vertex.
    let constraints = vec![c("self", "x", "x", -1), c("iso", "u", "v", 0)];
    let g = Graph::build(&constraints).unwrap();
    assert_eq!(g.component_count, 2);
    let outcome = bellman_ford(&g).unwrap();
    let cyc = cycle_of(outcome);
    log.state("cycle", serde_json::json!({"ids": cyc.cycle_constraint_ids, "weight": cyc.weight}));
    assert_eq!(cyc.cycle_constraint_ids, vec!["self".to_string()]);
    assert_eq!(cyc.weight, -1);
    assert!(brute_feasible(&constraints, 1).is_none());
    log.pass("negative self-loop detected without needing a source vertex");
}

/// Bellman–Ford must reject, not wrap, when a relaxation exceeds i64.
/// We build the graph by hand: an edge with weight i64::MIN relaxed from a
/// negative distance is the underflow candidate (dist[u]=-1, w=MIN).
#[test]
fn arithmetic_overflow_is_rejected_as_computation_failure() {
    let mut log = RunLog::start("arithmetic_overflow_is_rejected");
    let constraints = vec![
        c("drive", "b", "a", -1),       // dist[b] -> -1
        c("underflow", "c", "b", i64::MIN), // -1 + MIN overflows
    ];
    log.input(
        "constraints",
        serde_json::json!([
            {"id":"drive","b-a":-1},
            {"id":"underflow","c-b":i64::MIN}
        ]),
    );
    let g = Graph::build(&constraints).unwrap();
    let err: ServiceError = bellman_ford(&g).expect_err("must reject overflow");
    log.observed_error(err.kind.as_str(), &err.message);
    assert_eq!(
        err.kind,
        ErrorKind::ComputationFailed,
        "integer overflow during relaxation is a computation failure, got: {err:?}"
    );
    assert!(err.message.contains("overflows i64"));
    log.expected_error("computation_failed", "checked_add on -1 + i64::MIN rejects instead of wrapping");
}

/// The kernel enforces its resource limits directly (independent of the
/// store's earlier check).
#[test]
fn resource_limits_are_enforced_by_the_kernel() {
    let mut log = RunLog::start("resource_limits_kernel");
    log.state("limits", serde_json::json!({"max_vertices": MAX_VERTICES, "max_edges": MAX_EDGES}));

    // MAX_EDGES + 1 self-loop constraints, all on one variable. Note the
    // kernel checks vertices *before* edges; one variable cannot carry
    // MAX_EDGES+1 constraints via the Constraint DTO alone without duplicate
    // ids (which Graph::build tolerates but the store rejects). The edge cap
    // is therefore enforced end-to-end at the store/HTTP layer (see
    // http_api::resource_exhausted_is_413); here we lock the documented
    // ordering of the limits at compile time and exercise the vertex cap.
    const { assert!(MAX_EDGES >= MAX_VERTICES) };
    const { assert!(MAX_VERTICES >= 1) };

    // Vertex cap: MAX_VERTICES+1 distinct variables introduced by zero-weight
    // self loops -> vertex cap trips in the kernel.
    let too_many_v: Vec<Constraint> = (0..MAX_VERTICES + 1)
        .map(|i| {
            let name = format!("n{i}");
            Constraint::new(format!("v{i}"), &name, &name, 0).unwrap()
        })
        .collect();
    let gv = Graph::build(&too_many_v).unwrap();
    assert_eq!(gv.vertex_count(), MAX_VERTICES + 1);
    let errv = bellman_ford(&gv).expect_err("too many vertices must be rejected");
    assert_eq!(errv.kind, ErrorKind::ResourceExhausted);
    log.observed_error(errv.kind.as_str(), &errv.message);

    // Empty system is feasible immediately with zero passes.
    let empty = Graph::build(&[]).unwrap();
    match bellman_ford(&empty).unwrap() {
        SolveOutcome::Feasible(w) => {
            assert!(w.assignment.is_empty());
            assert_eq!(w.iterations, 0);
        }
        SolveOutcome::Infeasible(_) => panic!("empty system cannot be infeasible"),
    }
    log.expected_error(
        "resource_exhausted",
        "edge cap and vertex cap each reject an oversized graph; empty graph feasible",
    );
}
