//! 求解内核测试。
//!
//! 参考答案来自两处，均非被测内核生成：
//! 1. 用例中**手算**的具体赋值/环费用（见各用例注释的逐轮松弛过程）；
//! 2. `common::check_assignment` 朴素回代器与 `evidence::verify_cycle`
//!    （独立按输入语言重算图边，不调用 Bellman-Ford）。

mod common;

use std::collections::BTreeMap;

use common::*;
use diff_constraints_service::evidence::verify_cycle;
use diff_constraints_service::model::ConstraintInput;
use diff_constraints_service::solver::graph::SolverError;
use diff_constraints_service::solver::{build_graph, solve_trace, KernelOutcome};

fn assignment_map(
    graph: &diff_constraints_service::solver::Graph,
    values: &[i64],
) -> BTreeMap<String, i64> {
    graph
        .var_names
        .iter()
        .cloned()
        .zip(values.iter().copied())
        .collect()
}

/// 手算链：a-b<=5, b-c<=-2, c-a<=1。
///
/// 边（插入序）：e0: b->a(5), e1: c->b(-2), e2: a->c(1)。
/// 初始全 0（等价超源）：
///
/// - pass0: e1 松弛 d[b] = -2，其余不动；
/// - pass1: 三条边均不可松弛，提前收敛。
///
/// 手算可行赋值：a=0, b=-2, c=0。
#[test]
fn hand_computed_chain_exact_assignment() {
    let log = RunLog::new("hand_computed_chain_exact_assignment");
    let cs = vec![
        c("k1", "a", "b", 5),
        c("k2", "b", "c", -2),
        c("k3", "c", "a", 1),
    ];
    let graph = build_graph(&rows(&cs)).expect("graph build");
    log.state(
        "edges",
        (0..graph.edge_count())
            .map(|i| {
                let e = graph.edges[i];
                (
                    i,
                    graph.var_names[e.from].clone(),
                    graph.var_names[e.to].clone(),
                    e.weight,
                )
            })
            .collect::<Vec<_>>(),
    );

    let outcome = solve_trace(&graph).expect("solver ok");
    let (values, trace) = match outcome {
        KernelOutcome::Feasible { assignment, trace } => (assignment, trace),
        other => panic!("expected feasible, got {other:?}"),
    };
    log.state(
        "trace",
        trace
            .iter()
            .map(|p| (p.pass, p.detection, p.relaxations))
            .collect::<Vec<_>>(),
    );

    // 具体中间状态断言（不只是“有结果”）。
    assert_eq!(trace[0].relaxations, 1, "pass0 should relax exactly once");
    assert_eq!(trace[0].samples[0].current_dist, 0);
    assert_eq!(trace[0].samples[0].candidate_dist, -2);
    assert!(
        trace.iter().any(|p| p.detection),
        "must include detection pass"
    );

    let got = assignment_map(&graph, &values);
    log.state("assignment", &got);
    let expected = BTreeMap::from([
        ("a".to_string(), 0),
        ("b".to_string(), -2),
        ("c".to_string(), 0),
    ]);
    assert_eq!(
        got, expected,
        "assignment must equal the hand-computed reference"
    );

    // 独立回代：参考答案必须满足全部三条约束。
    check_assignment(&cs, &got).expect("independent check: all constraints satisfied");
    log.verdict(
        "feasible",
        "assignment equals hand-computed {a:0,b:-2,c:0} and passes independent substitution",
    );
}

/// 零权环 x-y<=0, y-x<=0 必须被判为**可行**，费用 0 不是冲突。
#[test]
fn zero_weight_cycle_is_feasible() {
    let log = RunLog::new("zero_weight_cycle_is_feasible");
    let cs = vec![c("z1", "x", "y", 0), c("z2", "y", "x", 0)];
    let graph = build_graph(&rows(&cs)).expect("graph build");
    let outcome = solve_trace(&graph).expect("solver ok");

    match outcome {
        KernelOutcome::Feasible { assignment, trace } => {
            let got = assignment_map(&graph, &assignment);
            log.state("assignment", &got);
            assert_eq!(
                trace[0].relaxations, 0,
                "no edge should relax from all-zero init"
            );
            assert!(
                got.values().all(|v| *v == 0),
                "zero-weight cycle admits all-zero potentials: {got:?}"
            );
            check_assignment(&cs, &got).expect("independent check");
            log.verdict(
                "feasible",
                "zero-cost cycle is not a negative cycle; all-zero assignment returned",
            );
        }
        KernelOutcome::Unsat { total_cost, .. } => {
            panic!("zero-weight cycle wrongly reported as conflict (cost={total_cost})");
        }
    }
}

/// 不连通分量：分量1 只有 a-b<=3；负环在分量2（p-q<=-1, q-p<=-1，费用 -2）。
/// 超源初始化保证远离起始点的负环同样被发现。
#[test]
fn negative_cycle_in_disconnected_component() {
    let log = RunLog::new("negative_cycle_in_disconnected_component");
    let cs = vec![
        c("g1", "a", "b", 3),
        c("n1", "p", "q", -1),
        c("n2", "q", "p", -1),
    ];
    let graph = build_graph(&rows(&cs)).expect("graph build");
    assert_eq!(
        graph.variable_count(),
        4,
        "two disconnected components, four variables"
    );

    let outcome = solve_trace(&graph).expect("solver ok");
    match outcome {
        KernelOutcome::Unsat {
            cycle_edges,
            total_cost,
            detection_pass,
        } => {
            log.state("cycle_edge_indices", &cycle_edges);
            assert_eq!(total_cost, -2, "hand-computed cycle cost: -1 + -1 = -2");
            assert!(total_cost < 0, "conflict cycle must be strictly negative");
            assert_eq!(
                cycle_edges.len(),
                2,
                "cycle uses exactly the two conflicting constraints"
            );
            assert_eq!(detection_pass, 3, "n=4 => detection pass is n-1=3");

            // 环闭合性：每条边的 to 等于下一条边的 from（首尾亦然）。
            for (i, &ei) in cycle_edges.iter().enumerate() {
                let e = graph.edges[ei];
                let next = graph.edges[cycle_edges[(i + 1) % cycle_edges.len()]];
                assert_eq!(
                    graph.var_names[e.to], graph.var_names[next.from],
                    "cycle must be closed at step {i}"
                );
            }

            // 证据只含命名约束，且就是 n1/n2（不含任何匿名超源边）。
            let names: Vec<String> = cycle_edges
                .iter()
                .map(|&ei| {
                    let src = graph.edges[ei].source;
                    rows(&cs)[src].0.clone()
                })
                .collect();
            log.state("cycle_constraint_names", &names);
            let name_set: std::collections::BTreeSet<_> = names.iter().collect();
            assert_eq!(
                name_set,
                std::collections::BTreeSet::from([&"n1".to_string(), &"n2".to_string()]),
                "evidence must cite the original constraint ids only"
            );

            // 独立复核（不经过内核）：
            let by_name: BTreeMap<_, _> = cs.iter().map(|k| (k.name.clone(), k.clone())).collect();
            let verified = verify_cycle(&names, &by_name)
                .expect("independent verify should not error")
                .expect("independent verify should accept the cycle");
            assert_eq!(verified.total_cost, -2);
            log.verdict(
                "unsat",
                "negative cycle in disconnected component found via super-source init; cost -2 confirmed independently",
            );
        }
        KernelOutcome::Feasible { .. } => panic!("expected unsat due to negative 2-cycle"),
    }
}

/// 单变量负自环 v-v<=-1：最短的负环（n=1 的检测轮边界）。
#[test]
fn self_loop_negative_cycle() {
    let log = RunLog::new("self_loop_negative_cycle");
    let cs = vec![c("s1", "v", "v", -1)];
    let graph = build_graph(&rows(&cs)).expect("graph build");
    let outcome = solve_trace(&graph).expect("solver ok");
    match outcome {
        KernelOutcome::Unsat {
            cycle_edges,
            total_cost,
            detection_pass,
        } => {
            assert_eq!(cycle_edges.len(), 1);
            assert_eq!(total_cost, -1);
            assert_eq!(detection_pass, 0, "n=1 => detection pass is 0");
            log.state("cycle_edges", &cycle_edges);
            log.verdict(
                "unsat",
                "self-loop weight -1 extracted on the detection pass",
            );
        }
        KernelOutcome::Feasible { .. } => panic!("self-loop v-v<=-1 must be unsat"),
    }
}

/// 三边形远端负环：a-b<=2, b-c<=2, c-a<=-5（费用 -1），验证取环顺序与闭合。
#[test]
fn three_edge_negative_cycle_far_from_sources() {
    let log = RunLog::new("three_edge_negative_cycle");
    let cs = vec![
        c("far1", "a", "b", 2),
        c("far2", "b", "c", 2),
        c("far3", "c", "a", -5),
    ];
    let graph = build_graph(&rows(&cs)).expect("graph build");
    let outcome = solve_trace(&graph).expect("solver ok");
    match outcome {
        KernelOutcome::Unsat {
            cycle_edges,
            total_cost,
            ..
        } => {
            let names: Vec<String> = cycle_edges
                .iter()
                .map(|&ei| rows(&cs)[graph.edges[ei].source].0.clone())
                .collect();
            log.state("cycle_names_in_walk_order", &names);
            assert_eq!(total_cost, -1, "2 + 2 + (-5) = -1");
            assert_eq!(
                names.iter().collect::<std::collections::BTreeSet<_>>(),
                std::collections::BTreeSet::from([
                    &"far1".to_string(),
                    &"far2".to_string(),
                    &"far3".to_string()
                ])
            );
            // 行走顺序闭合性。
            for (i, &ei) in cycle_edges.iter().enumerate() {
                let e = graph.edges[ei];
                let nxt = graph.edges[cycle_edges[(i + 1) % 3]];
                assert_eq!(e.to, nxt.from, "closed walk step {i}");
            }
            log.verdict(
                "unsat",
                "3-edge cycle cost -1 extracted in walk order and closed",
            );
        }
        KernelOutcome::Feasible { .. } => panic!("expected unsat (cycle cost -1)"),
    }
}

/// 空集合可行，赋值为空。
#[test]
fn empty_set_is_feasible() {
    let log = RunLog::new("empty_set_is_feasible");
    let graph = build_graph(&[]).expect("graph build");
    let outcome = solve_trace(&graph).expect("solver ok");
    match outcome {
        KernelOutcome::Feasible { assignment, .. } => {
            assert!(assignment.is_empty());
            log.verdict("feasible", "empty constraint set admits empty assignment");
        }
        other => panic!("expected feasible empty, got {other:?}"),
    }
}

/// 整数加法溢出必须被**明确拒绝**为计算失败，而不是 panic 或回绕。
///
/// o1: a-b<=-1 使 d[a]=-1；o2: x-a<=i64::MIN 在同一轮松弛计算
/// d[a] + MIN = -1 + i64::MIN，i64 下溢 -> ArithmeticOverflow。
#[test]
fn arithmetic_overflow_is_rejected_not_wrapped() {
    let log = RunLog::new("arithmetic_overflow_is_rejected_not_wrapped");
    let cs = vec![c("o1", "a", "b", -1), c("o2", "x", "a", i64::MIN)];
    let graph = build_graph(&rows(&cs)).expect("graph build");
    let err = solve_trace(&graph).expect_err("overflow must be an error, not a wrapped value");
    match err {
        SolverError::ArithmeticOverflow { where_ } => {
            log.state("overflow_at", &where_);
            assert!(
                where_.contains("relaxation"),
                "location must identify the relaxation: {where_}"
            );
            log.verdict(
                "computation_failure",
                "checked_add underflow reported with precise location",
            );
        }
        other => panic!("expected ArithmeticOverflow, got {other:?}"),
    }
}

/// 变量配额：129 个变量超过 128 上限 -> TooManyVariables。
#[test]
fn too_many_variables_is_resource_error() {
    let log = RunLog::new("too_many_variables_is_resource_error");
    let cs: Vec<ConstraintInput> = (0..128)
        .map(|i| {
            c(
                &format!("v{i}"),
                &format!("v{i}"),
                &format!("v{}", i + 1),
                0,
            )
        })
        .collect();
    let err = build_graph(&rows(&cs)).expect_err("129 variables must exceed quota");
    match err {
        SolverError::TooManyVariables { limit, got } => {
            assert_eq!((limit, got), (128, 129));
            log.state("quota", (limit, got));
            log.verdict(
                "resource_exhausted",
                "variable count 129 > limit 128 rejected before solving",
            );
        }
        other => panic!("expected TooManyVariables, got {other:?}"),
    }
}

/// 约束配额：1025 条边超过 1024 上限 -> TooManyConstraints。
#[test]
fn too_many_constraints_is_resource_error() {
    let log = RunLog::new("too_many_constraints_is_resource_error");
    let cs: Vec<ConstraintInput> = (0..1025)
        .map(|i| c(&format!("e{i:04}"), "v0", "v1", 0))
        .collect();
    let err = build_graph(&rows(&cs)).expect_err("1025 constraints must exceed quota");
    match err {
        SolverError::TooManyConstraints { limit, got } => {
            assert_eq!((limit, got), (1024, 1025));
            log.state("quota", (limit, got));
            log.verdict(
                "resource_exhausted",
                "constraint count 1025 > limit 1024 rejected",
            );
        }
        other => panic!("expected TooManyConstraints, got {other:?}"),
    }
}

/// 可重放的随机化交叉核验（种子写死）：
/// - 可行：独立回代器必须接受返回的赋值；
/// - 不可行：独立证据模块必须确认环严格负且费用一致。
#[test]
fn randomized_systems_cross_checked_independently() {
    let base_log = RunLog::new("randomized_systems_cross_checked_independently");
    let mut feasible_count = 0usize;
    let mut unsat_count = 0usize;

    for seed in 0..40u64 {
        let mut rng = Rng::new(1000 + seed);
        let nvars = 3 + rng.next_u64() % 4; // 3..=6 个变量
        let ncon = 4 + rng.next_u64() % 8; // 4..=11 条约束
        let var = |i: u64| format!("v{i}");
        let cs: Vec<ConstraintInput> = (0..ncon)
            .map(|i| {
                let x = var(rng.next_u64() % nvars);
                let y = var(rng.next_u64() % nvars);
                let w = rng.range(-3, 4);
                c(&format!("r{seed:02}_{i:02}"), &x, &y, w)
            })
            .collect();

        let graph = build_graph(&rows(&cs)).expect("small inputs within quota");
        let outcome = solve_trace(&graph).expect("small bounded weights cannot overflow");
        match outcome {
            KernelOutcome::Feasible { assignment, .. } => {
                let got = assignment_map(&graph, &assignment);
                check_assignment(&cs, &got).unwrap_or_else(|e| {
                    panic!("seed {seed}: kernel returned an infeasible assignment: {e}; assignment={got:?}")
                });
                feasible_count += 1;
            }
            KernelOutcome::Unsat {
                cycle_edges,
                total_cost,
                ..
            } => {
                assert!(
                    total_cost < 0,
                    "seed {seed}: reported cycle cost must be negative"
                );
                let names: Vec<String> = cycle_edges
                    .iter()
                    .map(|&ei| rows(&cs)[graph.edges[ei].source].0.clone())
                    .collect();
                let by_name: BTreeMap<_, _> =
                    cs.iter().map(|k| (k.name.clone(), k.clone())).collect();
                let dto = verify_cycle(&names, &by_name)
                    .expect("seed: verifier input errors")
                    .unwrap_or_else(|reason| {
                        panic!("seed {seed}: independent verifier rejected kernel cycle: {reason}")
                    });
                assert_eq!(dto.total_cost, total_cost, "seed {seed}: cost disagreement");
                eprintln!(
                    "[{}] seed={seed} variables={nvars} constraints={ncon} cycle={names:?} cost={total_cost}",
                    base_log.id
                );
                unsat_count += 1;
            }
        }
    }

    base_log.state("tally", (feasible_count, unsat_count));
    assert!(
        feasible_count > 0 && unsat_count > 0,
        "random suite should cover both outcomes"
    );
    base_log.verdict(
        "ok",
        "every feasible assignment passed independent substitution; every conflict cycle passed independent verification",
    );
}
