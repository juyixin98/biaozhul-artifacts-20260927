//! Solver kernel: weak trace inclusion of finite LTSs with silent steps.
//!
//! Algorithm
//! ---------
//! Trace inclusion is checked after *determinizing* each side. For an LTS with
//! silent steps, the deterministic macro-states are epsilon-closed sets:
//!
//! * `I_0 = eps(initial)` — states reachable before any observable action;
//! * `weak_post(S, a) = eps({ t | q -a-> t for some q in S })`.
//!
//! A macro-state accepts the prefix consumed so far iff one of its states is
//! accepting. The BFS explores pairs `(macro-spec, macro-impl)` reachable by
//! the same observable word. A pair `(S, I)` is a counterexample iff
//! `I` contains an accepting state while `S` does not. BFS by observable-word
//! length (ties broken by alphabetical action order) guarantees the returned
//! counterexample is the shortest, deterministically.
//!
//! Note the deliberate non-equivalence rule: we never merge two states just
//! because they offer the same single-step action. Acceptance and future
//! behavior are what matter; that is exactly what the set construction tracks.
//!
//! Bounds
//! ------
//! Every bound hit yields [`Verdict::Unknown`] with partial statistics, never a
//! false `not_included`.

use std::collections::{HashMap, VecDeque};

use serde::Serialize;

use crate::closure::ClosureTable;
use crate::error::{EngineError, EngineResult};
use crate::input::LimitsDef;
use crate::model::{LabelId, Lts, Pair, StateId};
use crate::witness::{self, Replay};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    Included,
    NotIncluded,
    Unknown,
}

/// Why an unknown verdict was returned.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "reason", rename_all = "snake_case")]
pub enum UnknownReason {
    /// BFS explored the configured maximum number of macro-state pairs.
    PairLimit { limit: usize },
    /// Determinization produced a macro-set over the configured size.
    SetSizeLimit { limit: usize, side: String },
    /// Witness reconstruction exceeded the edge budget.
    WitnessLimit { limit: usize },
}

#[derive(Debug, Clone, Serialize)]
pub struct Counterexample {
    /// Shortest observable word the implementation can produce (and accept) but
    /// the specification cannot accept.
    pub trace: Vec<String>,
    /// Same word as label ids (used to build the replay; not serialized).
    #[serde(skip_serializing)]
    pub trace_ids: Vec<LabelId>,
    /// Concrete accepting run of the implementation, including internal steps.
    pub implementation_replay: Replay,
    /// Macro-set pairs along the BFS path (root first).
    pub spec_path: Vec<Vec<String>>,
    pub impl_path: Vec<Vec<String>>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Stats {
    pub explored_pairs: usize,
    pub spec_macrostates: usize,
    pub impl_macrostates: usize,
    pub max_spec_set_size: usize,
    pub max_impl_set_size: usize,
    pub transitions_followed: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct CheckOutcome {
    pub verdict: Verdict,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub counterexample: Option<Counterexample>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub unknown: Option<UnknownReason>,
    pub stats: Stats,
}

/// Macro-state id within one side.
type SetId = u32;

#[derive(Default)]
struct SetPool {
    sets: Vec<Vec<StateId>>,
    index: HashMap<Vec<StateId>, SetId>,
}

impl SetPool {
    fn intern(&mut self, set: Vec<StateId>) -> SetId {
        if let Some(&id) = self.index.get(&set) {
            return id;
        }
        let id = self.sets.len() as SetId;
        self.index.insert(set.clone(), id);
        self.sets.push(set);
        id
    }
}

/// A BFS node over pairs of macro-states.
struct Node {
    spec: SetId,
    impl_: SetId,
    /// Arena index of the node reached by the one-shorter prefix, or `NONE`.
    parent: u32,
    /// Observable action that led from `parent` to this node.
    action: LabelId,
}

const NONE: u32 = u32::MAX;

struct Kernel<'a> {
    pair: &'a Pair,
    spec_closure: ClosureTable<'a>,
    impl_closure: ClosureTable<'a>,
    spec_sets: SetPool,
    impl_sets: SetPool,
    stats: Stats,
}

impl<'a> Kernel<'a> {
    /// `eps({ t | q -a-> t, q in set })` — observable weak successor macro-set.
    fn weak_post(&self, spec_side: bool, set: &[StateId], action: LabelId) -> Vec<StateId> {
        let (lts, closures): (&Lts, &ClosureTable<'_>) = if spec_side {
            (&self.pair.spec, &self.spec_closure)
        } else {
            (&self.pair.impl_, &self.impl_closure)
        };
        let mut seen = vec![false; lts.state_count()];
        for &q in set {
            for e in &lts.outgoing[q as usize] {
                if e.label == action {
                    for &t in closures.of(e.target) {
                        seen[t as usize] = true;
                    }
                }
            }
        }
        seen.into_iter()
            .zip(0..)
            .filter_map(|(v, id)| v.then_some(id))
            .collect()
    }

    fn set_accepts(lts: &Lts, set: &[StateId]) -> bool {
        set.iter().any(|&s| lts.accepting[s as usize])
    }

    /// Macro-set rendered with original state names.
    fn render_set(&self, spec_side: bool, id: SetId) -> Vec<String> {
        let lts = if spec_side {
            &self.pair.spec
        } else {
            &self.pair.impl_
        };
        let set = if spec_side {
            &self.spec_sets.sets[id as usize]
        } else {
            &self.impl_sets.sets[id as usize]
        };
        set.iter().map(|&s| lts.name_of_state(s).to_string()).collect()
    }
}

/// Run the inclusion check.
pub fn check(pair: &Pair, limits: &LimitsDef) -> EngineResult<CheckOutcome> {
    let mut k = Kernel {
        pair,
        spec_closure: ClosureTable::build(&pair.spec),
        impl_closure: ClosureTable::build(&pair.impl_),
        spec_sets: SetPool::default(),
        impl_sets: SetPool::default(),
        stats: Stats {
            explored_pairs: 0,
            spec_macrostates: 0,
            impl_macrostates: 0,
            max_spec_set_size: 0,
            max_impl_set_size: 0,
            transitions_followed: 0,
        },
    };

    // Initial macro-states: epsilon closure of each initial state.
    let spec_start_set = k.spec_closure.of(pair.spec.initial).to_vec();
    let impl_start_set = k.impl_closure.of(pair.impl_.initial).to_vec();
    let spec_start = k.spec_sets.intern(spec_start_set);
    let impl_start = k.impl_sets.intern(impl_start_set);

    // Arena of BFS nodes + FIFO queue of arena indices.
    let mut nodes: Vec<Node> = Vec::new();
    let mut queue: VecDeque<u32> = VecDeque::new();
    let mut seen: HashMap<(SetId, SetId), u32> = HashMap::new();
    let root = 0u32;
    nodes.push(Node {
        spec: spec_start,
        impl_: impl_start,
        parent: NONE,
        action: LabelId::MAX,
    });
    seen.insert((spec_start, impl_start), root);
    queue.push_back(root);

    let mut unknown: Option<UnknownReason> = None;
    let mut goal: Option<u32> = None;

    'bfs: while let Some(node_id) = queue.pop_front() {
        k.stats.explored_pairs += 1;
        if k.stats.explored_pairs > limits.max_explored_pairs {
            unknown = Some(UnknownReason::PairLimit {
                limit: limits.max_explored_pairs,
            });
            break;
        }

        let (sp, ip) = {
            let n = &nodes[node_id as usize];
            (n.spec, n.impl_)
        };
        let (spec_set, impl_set) = (
            k.spec_sets.sets[sp as usize].clone(),
            k.impl_sets.sets[ip as usize].clone(),
        );
        k.stats.max_spec_set_size = k.stats.max_spec_set_size.max(spec_set.len());
        k.stats.max_impl_set_size = k.stats.max_impl_set_size.max(impl_set.len());

        let spec_accepts = Kernel::set_accepts(&pair.spec, &spec_set);
        let impl_accepts = Kernel::set_accepts(&pair.impl_, &impl_set);
        if impl_accepts && !spec_accepts {
            goal = Some(node_id);
            break 'bfs;
        }

        // Observable actions are label ids 0..alphabet size (names were sorted
        // at compile time), so traversal order is deterministic.
        for action in 0..pair.label_names.len() as LabelId {
            let nspec = k.weak_post(true, &spec_set, action);
            let nimpl = k.weak_post(false, &impl_set, action);
            k.stats.transitions_followed += 1;

            if nspec.len() > limits.max_states_per_lts {
                unknown = Some(UnknownReason::SetSizeLimit {
                    limit: limits.max_states_per_lts,
                    side: pair.spec.name.clone(),
                });
                break 'bfs;
            }
            if nimpl.len() > limits.max_states_per_lts {
                unknown = Some(UnknownReason::SetSizeLimit {
                    limit: limits.max_states_per_lts,
                    side: pair.impl_.name.clone(),
                });
                break 'bfs;
            }

            let nsp = k.spec_sets.intern(nspec);
            let nip = k.impl_sets.intern(nimpl);
            if !seen.contains_key(&(nsp, nip)) {
                let id = nodes.len() as u32;
                nodes.push(Node {
                    spec: nsp,
                    impl_: nip,
                    parent: node_id,
                    action,
                });
                seen.insert((nsp, nip), id);
                queue.push_back(id);
            }
        }
    }

    k.stats.spec_macrostates = k.spec_sets.sets.len();
    k.stats.impl_macrostates = k.impl_sets.sets.len();

    let Some(goal_id) = goal else {
        return Ok(CheckOutcome {
            verdict: if unknown.is_some() {
                Verdict::Unknown
            } else {
                Verdict::Included
            },
            counterexample: None,
            unknown,
            stats: k.stats,
        });
    };

    // Reconstruct the observable word (root -> goal) and the macro-set path.
    let mut rev_actions: Vec<LabelId> = Vec::new();
    let mut rev_path: Vec<(SetId, SetId)> = Vec::new();
    let mut cur = goal_id;
    while cur != root {
        let n = &nodes[cur as usize];
        rev_actions.push(n.action);
        rev_path.push((n.spec, n.impl_));
        cur = n.parent;
    }
    rev_path.push((spec_start, impl_start));
    rev_actions.reverse();
    rev_path.reverse();

    let trace_ids = rev_actions;
    let trace: Vec<String> = trace_ids
        .iter()
        .map(|&l| pair.label_name(l).to_string())
        .collect();

    let mut spec_path = Vec::with_capacity(rev_path.len());
    let mut impl_path = Vec::with_capacity(rev_path.len());
    for (sp, ip) in &rev_path {
        spec_path.push(k.render_set(true, *sp));
        impl_path.push(k.render_set(false, *ip));
    }

    // Build the concrete, replayable accepting run of the implementation.
    let replay = match witness::build_accepting_run(
        &pair.impl_,
        &mut k.impl_closure,
        &trace_ids,
        &pair.silent_name,
        &|l| pair.label_name(l).to_string(),
        limits.max_witness_edges,
    )? {
        Some(r) => r,
        None => {
            // The BFS proved such a run exists; failing to reconstruct it is
            // an internal error, not an unknown verdict.
            return Err(EngineError::internal(
                "witness_reconstruction_failed",
                "macro-set BFS found acceptance but no concrete run could be rebuilt",
            ));
        }
    };
    if replay.edge_count > limits.max_witness_edges {
        return Ok(CheckOutcome {
            verdict: Verdict::Unknown,
            counterexample: None,
            unknown: Some(UnknownReason::WitnessLimit {
                limit: limits.max_witness_edges,
            }),
            stats: k.stats,
        });
    }

    Ok(CheckOutcome {
        verdict: Verdict::NotIncluded,
        counterexample: Some(Counterexample {
            trace,
            trace_ids,
            implementation_replay: replay,
            spec_path,
            impl_path,
        }),
        unknown: None,
        stats: k.stats,
    })
}
