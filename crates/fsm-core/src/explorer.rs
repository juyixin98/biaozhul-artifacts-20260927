//! Explicit-state exploration kernel.
//!
//! BFS over the reachable state graph starting from every state satisfying
//! the initial predicate. BFS guarantees the shortest counterexample /
//! witness path. Transitions are considered in declaration order, giving a
//! deterministic first-shortest path. States are deduplicated by their
//! canonical encoded assignment.
//!
//! Exploration is bounded by a [`Budget`]. When the budget is exhausted the
//! run is reported as `truncated` and every property verdict is UNKNOWN —
//! absence of a counterexample under truncation is never reported as proof.

use fsm_lang::compile::CompiledSpec;
use fsm_lang::error::StateError;
use fsm_lang::evidence::{kind, Evidence, NamedValue, TraceStep};
use fsm_lang::model::{PropertyKind, Value};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, VecDeque};

/// Outcome reason codes.
pub mod reason {
    pub const NO_INITIAL_STATE: &str = "NO_INITIAL_STATE";
    pub const PROVED: &str = "PROVED";
    pub const COUNTEREXAMPLE_FOUND: &str = "COUNTEREXAMPLE_FOUND";
    pub const WITNESS_FOUND: &str = "WITNESS_FOUND";
    pub const TARGET_UNREACHABLE: &str = "TARGET_UNREACHABLE";
    pub const BUDGET_TRUNCATED: &str = "BUDGET_TRUNCATED";
    pub const INITIAL_SCAN_TRUNCATED: &str = "INITIAL_SCAN_TRUNCATED";
    pub const EVALUATION_ERROR: &str = "EVALUATION_ERROR";
}

/// Property verdicts.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    /// AG holds / EF target reachable, within a fully explored graph.
    True,
    /// AG violated (counterexample) / EF target unreachable over full graph.
    False,
    /// Exploration was truncated; nothing definitive can be concluded.
    Unknown,
}

/// Overall run status.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RunStatus {
    /// Graph fully explored within budget.
    Complete,
    /// Budget exhausted; verdicts are unknown (unless a witness/violation
    /// was already concretely found).
    Truncated,
    /// A model evaluation error (overflow, out-of-domain assignment)
    /// occurred; results up to that point are reported but invalid.
    Error,
    /// Specification had no initial state.
    Invalid,
}

/// Exploration budget.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Budget {
    /// Maximum number of distinct states popped and expanded.
    pub max_states: u64,
    /// Maximum number of transition evaluations (guards tested).
    pub max_transitions: u64,
    /// Maximum number of full-domain assignments scanned while finding
    /// initial states.
    pub max_initial_scan: u64,
}

impl Default for Budget {
    fn default() -> Self {
        Budget {
            max_states: 100_000,
            max_transitions: 1_000_000,
            max_initial_scan: 1_000_000,
        }
    }
}

/// Exploration counters.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct Stats {
    /// Distinct reachable states discovered (including the initial set).
    pub discovered: u64,
    /// States popped from the queue and fully processed.
    pub explored: u64,
    /// Transition guard evaluations performed.
    pub transition_evaluations: u64,
    /// Transitions that fired (guard true, target computed).
    pub transitions_taken: u64,
    /// Domain assignments scanned while searching for initial states.
    pub initial_scanned: u64,
    /// Initial states found.
    pub initial_states: u64,
    /// Reachable states with no enabled transition and not legal-terminal.
    pub deadlocks: u64,
    /// Reachable states satisfying the legal-termination predicate.
    pub terminal_states: u64,
    /// Total Cartesian-product size (informational; may be 0 on overflow).
    pub total_domain_states: u128,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PropertyResult {
    pub name: String,
    pub kind: PropertyKind,
    pub verdict: Verdict,
    /// Machine-readable outcome reason.
    pub reason: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub evidence: Option<Evidence>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunError {
    pub code: String,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub transition: Option<String>,
}

/// Full result of one model-checking run.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExploreOutcome {
    pub status: RunStatus,
    pub reason: String,
    pub stats: Stats,
    #[serde(default)]
    pub properties: Vec<PropertyResult>,
    /// Shortest deadlock traces (at most one per distinct deadlock state;
    /// capped to keep output bounded).
    #[serde(default)]
    pub deadlock_evidence: Vec<Evidence>,
    /// Shortest legal-terminal traces (capped).
    #[serde(default)]
    pub terminal_evidence: Vec<Evidence>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<RunError>,
    pub truncated: bool,
}

const MAX_STRUCTURAL_TRACES: usize = 16;

struct Node {
    state: Vec<Value>,
    parent: Option<usize>,
    fired: Option<String>,
}

/// Run explicit-state exploration on a compiled specification.
pub fn explore(spec: &CompiledSpec, budget: &Budget) -> ExploreOutcome {
    // ---- enumerate initial states over the declared domain ----
    let mut nodes: Vec<Node> = Vec::new();
    let mut index_of: HashMap<Vec<Value>, usize> = HashMap::new();
    let mut queue: VecDeque<usize> = VecDeque::new();
    let mut stats = Stats {
        total_domain_states: spec.total_states(),
        ..Stats::default()
    };

    let mut domain_iter = spec.iter_domain();
    let mut scan_exhausted = true;
    for candidate in domain_iter.by_ref() {
        if stats.initial_scanned == budget.max_initial_scan {
            // Budget fully consumed while the domain still has elements.
            scan_exhausted = false;
            break;
        }
        stats.initial_scanned += 1;
        match spec.initial.eval_bool(&candidate) {
            Ok(true) => {
                // Domain enumeration yields each assignment once, so every
                // satisfying candidate is a distinct initial state.
                let id = nodes.len();
                index_of.insert(candidate.clone(), id);
                nodes.push(Node {
                    state: candidate,
                    parent: None,
                    fired: None,
                });
                queue.push_back(id);
                stats.initial_states += 1;
                stats.discovered += 1;
            }
            Ok(false) => {}
            Err(e) => {
                return error_outcome(
                    stats,
                    RunError {
                        code: e.code,
                        message: format!("initial predicate: {}", e.message),
                        transition: None,
                    },
                );
            }
        }
    }
    let initial_scan_truncated = !scan_exhausted;

    if nodes.is_empty() {
        let code = if initial_scan_truncated {
            reason::INITIAL_SCAN_TRUNCATED
        } else {
            reason::NO_INITIAL_STATE
        };
        return ExploreOutcome {
            status: if initial_scan_truncated {
                RunStatus::Truncated
            } else {
                RunStatus::Invalid
            },
            reason: code.to_string(),
            stats,
            properties: vec![],
            deadlock_evidence: vec![],
            terminal_evidence: vec![],
            error: None,
            truncated: initial_scan_truncated,
        };
    }

    // Precompute where each property stores its first-found node.
    let mut ag_slot_of: Vec<Option<usize>> = Vec::with_capacity(spec.properties.len());
    let mut ef_slot_of: Vec<Option<usize>> = Vec::with_capacity(spec.properties.len());
    let mut ag_count = 0usize;
    let mut ef_count = 0usize;
    for prop in &spec.properties {
        match prop.kind {
            PropertyKind::Ag => {
                ag_slot_of.push(Some(ag_count));
                ef_slot_of.push(None);
                ag_count += 1;
            }
            PropertyKind::Ef => {
                ag_slot_of.push(None);
                ef_slot_of.push(Some(ef_count));
                ef_count += 1;
            }
        }
    }
    let mut ag_found = vec![usize::MAX; ag_count];
    let mut ef_found = vec![usize::MAX; ef_count];
    let mut deadlock_nodes: Vec<usize> = Vec::new();
    let mut terminal_nodes: Vec<usize> = Vec::new();
    let mut fatal: Option<RunError> = None;
    let mut states_truncated = false;
    let mut transitions_truncated = false;

    'bfs: loop {
        // Check the state budget before consuming the next queued node:
        // a node still sitting in the queue when the budget is exhausted
        // means the reachable graph was not fully explored.
        if stats.explored >= budget.max_states {
            if !queue.is_empty() {
                states_truncated = true;
            }
            break;
        }
        let Some(nid) = queue.pop_front() else { break };
        stats.explored += 1;
        let state = nodes[nid].state.clone();

        // One predicate evaluation per property on this state. BFS order
        // means the first violating/satisfying state is shortest.
        for (pi, prop) in spec.properties.iter().enumerate() {
            let holds = match prop.predicate.eval_bool(&state) {
                Ok(h) => h,
                Err(e) => {
                    fatal = Some(RunError {
                        code: e.code,
                        message: format!("property `{}`: {}", prop.name, e.message),
                        transition: None,
                    });
                    break 'bfs;
                }
            };
            match prop.kind {
                PropertyKind::Ag if !holds => {
                    let slot = ag_slot_of[pi].unwrap();
                    if ag_found[slot] == usize::MAX {
                        ag_found[slot] = nid;
                    }
                }
                PropertyKind::Ef if holds => {
                    let slot = ef_slot_of[pi].unwrap();
                    if ef_found[slot] == usize::MAX {
                        ef_found[slot] = nid;
                    }
                }
                _ => {}
            }
        }

        // Legal terminal states are not expanded.
        let mut is_terminal = false;
        if let Some(term) = &spec.terminal {
            match term.eval_bool(&state) {
                Ok(true) => {
                    is_terminal = true;
                    stats.terminal_states += 1;
                    if terminal_nodes.len() < MAX_STRUCTURAL_TRACES {
                        terminal_nodes.push(nid);
                    }
                }
                Ok(false) => {}
                Err(e) => {
                    fatal = Some(RunError {
                        code: e.code,
                        message: format!("terminal predicate: {}", e.message),
                        transition: None,
                    });
                    break 'bfs;
                }
            }
        }
        if is_terminal {
            continue;
        }

        let mut enabled = 0u64;
        for (ti, t) in spec.transitions.iter().enumerate() {
            if stats.transition_evaluations >= budget.max_transitions {
                // Remaining work exists iff this state has more guards or
                // other states are already queued.
                let remaining_here = spec.transitions.len() - ti;
                if remaining_here > 0 || !queue.is_empty() {
                    transitions_truncated = true;
                }
                break;
            }
            stats.transition_evaluations += 1;
            let guard = match t.guard.eval_bool(&state) {
                Ok(g) => g,
                Err(e) => {
                    fatal = Some(RunError {
                        code: e.code,
                        message: format!("guard of `{}`: {}", t.name, e.message),
                        transition: Some(t.name.clone()),
                    });
                    break 'bfs;
                }
            };
            if !guard {
                continue;
            }
            enabled += 1;
            let next = match spec.apply(t, &state) {
                Ok(s) => s,
                Err(StateError {
                    code,
                    message,
                    transition,
                }) => {
                    fatal = Some(RunError {
                        code,
                        message,
                        transition: Some(transition),
                    });
                    break 'bfs;
                }
            };
            stats.transitions_taken += 1;
            if !index_of.contains_key(&next) {
                let child = nodes.len();
                index_of.insert(next.clone(), child);
                nodes.push(Node {
                    state: next,
                    parent: Some(nid),
                    fired: Some(t.name.clone()),
                });
                queue.push_back(child);
                stats.discovered += 1;
            }
        }
        if transitions_truncated {
            break;
        }
        if enabled == 0 {
            // No enabled transition and not legal-terminal => deadlock.
            stats.deadlocks += 1;
            if deadlock_nodes.len() < MAX_STRUCTURAL_TRACES {
                deadlock_nodes.push(nid);
            }
        }
    }

    if let Some(err) = fatal {
        return error_outcome(stats, err);
    }

    let truncated = states_truncated
        || transitions_truncated
        || initial_scan_truncated
        || !queue.is_empty();
    // ---- build per-property results ----
    let mut properties = Vec::with_capacity(spec.properties.len());
    let mut ag_slot = 0usize;
    let mut ef_slot = 0usize;
    for prop in &spec.properties {
        match prop.kind {
            PropertyKind::Ag => {
                let node = ag_found[ag_slot];
                let (verdict, reason_code, evidence) = if node != usize::MAX {
                    (
                        Verdict::False,
                        reason::COUNTEREXAMPLE_FOUND,
                        Some(build_evidence(
                            kind::AG_VIOLATION,
                            &prop.name,
                            node,
                            &nodes,
                            spec,
                        )),
                    )
                } else if truncated {
                    (Verdict::Unknown, reason::BUDGET_TRUNCATED, None)
                } else {
                    (Verdict::True, reason::PROVED, None)
                };
                properties.push(PropertyResult {
                    name: prop.name.clone(),
                    kind: PropertyKind::Ag,
                    verdict,
                    reason: reason_code.to_string(),
                    evidence,
                });
                ag_slot += 1;
            }
            PropertyKind::Ef => {
                let node = ef_found[ef_slot];
                let (verdict, reason_code, evidence) = if node != usize::MAX {
                    (
                        Verdict::True,
                        reason::WITNESS_FOUND,
                        Some(build_evidence(
                            kind::EF_WITNESS,
                            &prop.name,
                            node,
                            &nodes,
                            spec,
                        )),
                    )
                } else if truncated {
                    (Verdict::Unknown, reason::BUDGET_TRUNCATED, None)
                } else {
                    (Verdict::False, reason::TARGET_UNREACHABLE, None)
                };
                properties.push(PropertyResult {
                    name: prop.name.clone(),
                    kind: PropertyKind::Ef,
                    verdict,
                    reason: reason_code.to_string(),
                    evidence,
                });
                ef_slot += 1;
            }
        }
    }

    let deadlock_evidence = deadlock_nodes
        .into_iter()
        .map(|n| build_evidence(kind::DEADLOCK, "", n, &nodes, spec))
        .collect();
    let terminal_evidence = terminal_nodes
        .into_iter()
        .map(|n| build_evidence(kind::TERMINAL, "", n, &nodes, spec))
        .collect();

    let (status, reason_code) = if truncated {
        let r = if initial_scan_truncated {
            reason::INITIAL_SCAN_TRUNCATED
        } else {
            reason::BUDGET_TRUNCATED
        };
        (RunStatus::Truncated, r)
    } else {
        (RunStatus::Complete, reason::PROVED)
    };

    ExploreOutcome {
        status,
        reason: reason_code.to_string(),
        stats,
        properties,
        deadlock_evidence,
        terminal_evidence,
        error: None,
        truncated,
    }
}

fn error_outcome(stats: Stats, error: RunError) -> ExploreOutcome {
    ExploreOutcome {
        status: RunStatus::Error,
        reason: reason::EVALUATION_ERROR.to_string(),
        stats,
        properties: vec![],
        deadlock_evidence: vec![],
        terminal_evidence: vec![],
        error: Some(error),
        truncated: false,
    }
}

fn build_evidence(
    kind_code: &str,
    property: &str,
    node: usize,
    nodes: &[Node],
    spec: &CompiledSpec,
) -> Evidence {
    let mut path: Vec<(usize, Option<String>)> = Vec::new();
    let mut cur = Some(node);
    while let Some(n) = cur {
        path.push((n, nodes[n].fired.clone()));
        cur = nodes[n].parent;
    }
    path.reverse();
    let trace = path
        .into_iter()
        .enumerate()
        .map(|(index, (n, fired))| TraceStep {
            index,
            state: nodes[n]
                .state
                .iter()
                .zip(&spec.variables)
                .map(|(v, var)| NamedValue {
                    var: var.name.clone(),
                    value: v.clone(),
                })
                .collect(),
            fired,
        })
        .collect();
    Evidence {
        kind: kind_code.to_string(),
        property: property.to_string(),
        trace,
    }
}
