//! Budgeted breadth-first search kernel.
//!
//! Budget semantics (`max_states`): the budget bounds the number of **distinct
//! states consumed** — initial states plus newly discovered successors. The
//! queue always contains consumed states; they are all expanded unless the
//! budget is hit while a node is being expanded. If the budget is reached,
//! already-consumed queued states remain unexpanded, reachability is not
//! closed, and every undecided property reports `unknown`. A found witness is
//! always reported even under truncation.

use std::collections::{HashMap, VecDeque};
use std::time::Instant;

use serde::Serialize;

use fsm_lang::ast::Expr;
use fsm_lang::{System, Value};

use crate::evidence::{Evidence, EvidenceKind};
use crate::{Conclusion, Outcome, PropertyResult, RunStatus};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum QueryKind {
    /// `AG p`: p must hold in every reachable state.
    Ag,
    /// `EF p`: some reachable state satisfies p. Reported as `violated` with
    /// a witness when reachable, `holds` when the full reachable set has no
    /// target (the common "error state unreachable" reading).
    Ef,
    /// Deadlock-freedom.
    DeadlockFree,
}

#[derive(Debug, Clone)]
pub struct Query {
    pub name: String,
    pub kind: QueryKind,
    pub predicate: Option<Expr>,
    pub source: String,
}

#[derive(Debug, Clone, Copy)]
pub struct CheckOptions {
    pub max_states: u64,
}

impl Default for CheckOptions {
    fn default() -> Self {
        CheckOptions {
            max_states: 100_000,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RunErrorKind {
    /// Init predicate selects no state.
    NoInitialState,
    /// Budget ran out before the initial scan could prove no-init / find init.
    InitialEnumerationOverflow,
    /// Guard/update/predicate evaluation failed at runtime.
    EvalError,
    /// Specification failed to build.
    BuildError,
}

#[derive(Debug, Clone, Serialize)]
pub struct RunError {
    pub kind: RunErrorKind,
    pub detail: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_position: Option<(usize, usize)>,
}

impl RunError {
    fn eval(e: fsm_lang::EvalError, phase: &str) -> Self {
        RunError {
            kind: RunErrorKind::EvalError,
            detail: format!("{phase}: {e}"),
            source_position: None,
        }
    }
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct Stats {
    pub states_consumed: u64,
    pub states_explored: u64,
    pub transitions_evaluated: u64,
    pub transitions_taken: u64,
    pub initial_states: u64,
    pub initial_candidates_scanned: u64,
    pub terminal_states: u64,
    pub deadlocked_states: u64,
    pub reachability_closed: bool,
    pub elapsed_ms: u128,
}

struct Parent {
    parent: u64,
    fired: String,
}

struct Finding {
    state: u64,
}

impl Clone for Finding {
    fn clone(&self) -> Self {
        Finding { state: self.state }
    }
}

/// Marker used as the early-return carrier inside `run_check`, which always
/// produces a structured [`Outcome`] rather than panicking.
struct RunFailed(RunError);

pub fn run_check(sys: &System, queries: &[Query], opts: CheckOptions) -> Outcome {
    let started = Instant::now();
    let mut trace: Vec<String> = vec![format!(
        "kernel v{}: budgeted BFS start, max_states={}",
        crate::VERSION,
        opts.max_states
    )];
    let mut stats = Stats::default();
    let budget = opts.max_states;

    let finish = |result: Result<OutcomeInputs, RunFailed>,
                  stats: Stats,
                  mut trace: Vec<String>|
     -> Outcome {
        let mut s = stats;
        s.elapsed_ms = started.elapsed().as_millis();
        match result {
            Ok(inp) => {
                let status = if s.reachability_closed {
                    RunStatus::Complete
                } else {
                    RunStatus::Truncated
                };
                trace.push(format!(
                    "finished {status:?}: consumed={}, explored={}, closed={}, elapsed_ms={}",
                    s.states_consumed, s.states_explored, s.reachability_closed, s.elapsed_ms
                ));
                let deadlock_evidence = inp
                    .deadlock
                    .clone()
                    .map(|f| evidence(sys, EvidenceKind::Deadlock, f, &inp.visited));
                let props = assemble_properties(queries, inp, status, budget, sys, &mut trace);
                Outcome {
                    status,
                    properties: props,
                    deadlock_found: deadlock_evidence.is_some(),
                    deadlock_evidence,
                    stats: s,
                    trace,
                    error: None,
                }
            }
            Err(RunFailed(err)) => {
                trace.push(format!("error {:?}: {}", err.kind, err.detail));
                Outcome {
                    status: RunStatus::Error,
                    properties: Vec::new(),
                    deadlock_found: false,
                    deadlock_evidence: None,
                    stats: s,
                    trace,
                    error: Some(err),
                }
            }
        }
    };

    let inputs = match explore(sys, queries, opts, &mut stats, &mut trace) {
        Ok(i) => i,
        Err(e) => return finish(Err(e), stats, trace),
    };
    finish(Ok(inputs), stats, trace)
}

struct OutcomeInputs {
    visited: HashMap<u64, Option<Parent>>,
    ag_violation: Vec<Option<Finding>>,
    ef_hit: Vec<Option<Finding>>,
    deadlock: Option<Finding>,
}

#[allow(clippy::too_many_arguments)]
fn explore(
    sys: &System,
    queries: &[Query],
    opts: CheckOptions,
    stats: &mut Stats,
    trace: &mut Vec<String>,
) -> Result<OutcomeInputs, RunFailed> {
    let budget = opts.max_states;
    let mut visited: HashMap<u64, Option<Parent>> = HashMap::new();
    let mut queue: VecDeque<u64> = VecDeque::new();
    let mut truncated = false;

    // ---- initial enumeration --------------------------------------------
    // Two cases:
    //  * Explicit concrete initial state: that single valuation is the only
    //    root; no product scan is required (works even for large domains).
    //  * Initial predicate: the finite product is scanned to decide whether
    //    *any* initial state exists. Scanning a valuation to test the
    //    predicate does not consume it against the reachable-state budget;
    //    only states actually selected count. Build time already bounds the
    //    product to the u64 mixed-radix codec capacity.
    let total_space = sys.total_space();

    if let Some(init_st) = sys.concrete_init.clone() {
        let code = sys.encode(&init_st);
        stats.initial_candidates_scanned = 1;
        visited.insert(code, None);
        queue.push_back(code);
        stats.initial_states = 1;
        stats.states_consumed = 1;
    } else {
        let init_expr = sys.init.clone();
        for n in 0u128..total_space {
            let code = u64::try_from(n).expect("product domain bounded by codec at build time");
            let st = sys.decode(code);
            stats.initial_candidates_scanned += 1;
            let is_init = match fsm_lang::eval::eval(sys, &init_expr, &st) {
                Ok(Value::Bool(b)) => b,
                Ok(_) => {
                    return Err(RunFailed(RunError {
                        kind: RunErrorKind::EvalError,
                        detail: "init predicate did not return a boolean".into(),
                        source_position: None,
                    }))
                }
                Err(e) => return Err(RunFailed(RunError::eval(e, "init predicate"))),
            };
            if is_init && visited.insert(code, None).is_none() {
                if stats.states_consumed >= budget {
                    truncated = true;
                    break;
                }
                queue.push_back(code);
                stats.initial_states += 1;
                stats.states_consumed += 1;
            }
        }
    }

    if visited.is_empty() {
        return Err(RunFailed(RunError {
            kind: RunErrorKind::NoInitialState,
            detail: "init predicate selects no state in the declared product domain".into(),
            source_position: None,
        }));
    }
    trace.push(format!(
        "initial enumeration: {} root(s) from {} candidate(s)",
        stats.initial_states, stats.initial_candidates_scanned
    ));

    let mut ag_violation: Vec<Option<Finding>> = vec![None; queries.len()];
    let mut ef_hit: Vec<Option<Finding>> = vec![None; queries.len()];
    let mut deadlock: Option<Finding> = None;

    let eval_pred = |q: &Query, st: &[Value]| -> Result<bool, RunFailed> {
        let pred = q.predicate.as_ref().expect("predicate query has expr");
        match fsm_lang::eval::eval(sys, pred, st) {
            Ok(Value::Bool(b)) => Ok(b),
            Ok(_) => Err(RunFailed(RunError {
                kind: RunErrorKind::EvalError,
                detail: format!("property '{}' did not return a boolean", q.name),
                source_position: None,
            })),
            Err(e) => Err(RunFailed(RunError::eval(
                e,
                &format!("property '{}'", q.name),
            ))),
        }
    };

    let evaluate_state = |code: u64,
                          st: &[Value],
                          ag: &mut [Option<Finding>],
                          ef: &mut [Option<Finding>]|
     -> Result<(), RunFailed> {
        for (i, q) in queries.iter().enumerate() {
            match q.kind {
                QueryKind::Ag if ag[i].is_none() && !eval_pred(q, st)? => {
                    ag[i] = Some(Finding { state: code });
                }
                QueryKind::Ef if ef[i].is_none() && eval_pred(q, st)? => {
                    ef[i] = Some(Finding { state: code });
                }
                _ => {}
            }
        }
        Ok(())
    };

    // properties at roots
    for &code in &queue.clone() {
        let st = sys.decode(code);
        evaluate_state(code, &st, &mut ag_violation, &mut ef_hit)?;
    }

    // ---- BFS expansion ---------------------------------------------------
    while let Some(code) = queue.pop_front() {
        stats.states_explored += 1;
        let st = sys.decode(code);

        let terminal = sys
            .is_terminal(&st)
            .map_err(|e| RunFailed(RunError::eval(e, "terminal predicate")))?;
        if terminal {
            stats.terminal_states += 1;
        }

        let mut enabled_any = false;
        for t in &sys.transitions {
            stats.transitions_evaluated += 1;
            let guard = sys
                .guard_holds(t, &st)
                .map_err(|e| RunFailed(RunError::eval(e, &format!("guard '{}'", t.name))))?;
            if !guard {
                continue;
            }
            enabled_any = true;
            let next = sys
                .apply(t, &st)
                .map_err(|e| RunFailed(RunError::eval(e, &format!("update '{}'", t.name))))?;
            let nc = sys.encode(&next);
            if visited.contains_key(&nc) {
                continue;
            }
            if stats.states_consumed >= budget {
                truncated = true;
                break;
            }
            visited.insert(
                nc,
                Some(Parent {
                    parent: code,
                    fired: t.name.clone(),
                }),
            );
            stats.transitions_taken += 1;
            stats.states_consumed += 1;
            evaluate_state(nc, &next, &mut ag_violation, &mut ef_hit)?;
            queue.push_back(nc);
            if stats.states_consumed >= budget {
                truncated = true;
                break;
            }
        }

        if !enabled_any && !terminal {
            stats.deadlocked_states += 1;
            if deadlock.is_none() {
                deadlock = Some(Finding { state: code });
                trace.push(format!(
                    "deadlock found at state code {code}: 0 enabled transitions, terminal=false"
                ));
            }
        }

        if truncated {
            trace.push(format!(
                "budget reached: states_consumed={}/{}, states_explored={}",
                stats.states_consumed, budget, stats.states_explored
            ));
            break;
        }
    }

    stats.reachability_closed = !truncated && queue.is_empty();
    Ok(OutcomeInputs {
        visited,
        ag_violation,
        ef_hit,
        deadlock,
    })
}

fn build_chain(
    visited: &HashMap<u64, Option<Parent>>,
    sys: &System,
    target: u64,
) -> Vec<(Vec<Value>, Option<String>)> {
    let mut chain = Vec::new();
    let mut cur = target;
    loop {
        let st = sys.decode(cur);
        match visited.get(&cur) {
            Some(Some(p)) => {
                chain.push((st, Some(p.fired.clone())));
                cur = p.parent;
            }
            _ => {
                chain.push((st, None));
                break;
            }
        }
    }
    chain.reverse();
    chain
}

fn evidence(
    sys: &System,
    kind: EvidenceKind,
    f: Finding,
    visited: &HashMap<u64, Option<Parent>>,
) -> Evidence {
    let chain = build_chain(visited, sys, f.state);
    Evidence::from_chain(kind, sys, chain)
}

fn assemble_properties(
    queries: &[Query],
    inp: OutcomeInputs,
    status: RunStatus,
    budget: u64,
    sys: &System,
    trace: &mut Vec<String>,
) -> Vec<PropertyResult> {
    let closed = status == RunStatus::Complete;
    let mut out = Vec::new();
    for (i, q) in queries.iter().enumerate() {
        let pr = match q.kind {
            QueryKind::Ag => {
                if let Some(f) = &inp.ag_violation[i] {
                    PropertyResult {
                        name: q.name.clone(),
                        kind: QueryKind::Ag,
                        expr: q.source.clone(),
                        conclusion: Conclusion::Violated,
                        reason: Some("reachable state falsifies the invariant".into()),
                        evidence: Some(evidence(
                            sys,
                            EvidenceKind::AgViolation,
                            clone_f(f),
                            &inp.visited,
                        )),
                    }
                } else if closed {
                    PropertyResult {
                        name: q.name.clone(),
                        kind: QueryKind::Ag,
                        expr: q.source.clone(),
                        conclusion: Conclusion::Holds,
                        reason: Some(
                            "invariant held on every reachable state; reachability closed".into(),
                        ),
                        evidence: None,
                    }
                } else {
                    PropertyResult {
                        name: q.name.clone(),
                        kind: QueryKind::Ag,
                        expr: q.source.clone(),
                        conclusion: Conclusion::Unknown,
                        reason: Some(format!(
                            "budget of {budget} states exhausted before reachability closed; NOT proven"
                        )),
                        evidence: None,
                    }
                }
            }
            QueryKind::Ef => {
                if let Some(f) = &inp.ef_hit[i] {
                    PropertyResult {
                        name: q.name.clone(),
                        kind: QueryKind::Ef,
                        expr: q.source.clone(),
                        conclusion: Conclusion::Violated,
                        reason: Some("target state reachable".into()),
                        evidence: Some(evidence(
                            sys,
                            EvidenceKind::EfReachable,
                            clone_f(f),
                            &inp.visited,
                        )),
                    }
                } else if closed {
                    PropertyResult {
                        name: q.name.clone(),
                        kind: QueryKind::Ef,
                        expr: q.source.clone(),
                        conclusion: Conclusion::Holds,
                        reason: Some(
                            "target unreachable: full reachable set explored and none satisfied it"
                                .into(),
                        ),
                        evidence: None,
                    }
                } else {
                    PropertyResult {
                        name: q.name.clone(),
                        kind: QueryKind::Ef,
                        expr: q.source.clone(),
                        conclusion: Conclusion::Unknown,
                        reason: Some(
                            "budget exhausted before reachability closed; reachability undecided"
                                .into(),
                        ),
                        evidence: None,
                    }
                }
            }
            QueryKind::DeadlockFree => match &inp.deadlock {
                Some(f) => PropertyResult {
                    name: q.name.clone(),
                    kind: QueryKind::DeadlockFree,
                    expr: String::new(),
                    conclusion: Conclusion::Violated,
                    reason: Some(
                        "reachable non-terminal state with zero enabled transitions".into(),
                    ),
                    evidence: Some(evidence(
                        sys,
                        EvidenceKind::Deadlock,
                        clone_f(f),
                        &inp.visited,
                    )),
                },
                None if closed => PropertyResult {
                    name: q.name.clone(),
                    kind: QueryKind::DeadlockFree,
                    expr: String::new(),
                    conclusion: Conclusion::Holds,
                    reason: Some("no deadlock in the closed reachable set".into()),
                    evidence: None,
                },
                None => PropertyResult {
                    name: q.name.clone(),
                    kind: QueryKind::DeadlockFree,
                    expr: String::new(),
                    conclusion: Conclusion::Unknown,
                    reason: Some(
                        "budget exhausted; unexpanded frontier may contain deadlocks".into(),
                    ),
                    evidence: None,
                },
            },
        };
        trace.push(format!(
            "property '{}' ({:?}): {:?}",
            pr.name, pr.kind, pr.conclusion
        ));
        out.push(pr);
    }
    out
}

fn clone_f(f: &Finding) -> Finding {
    Finding { state: f.state }
}
