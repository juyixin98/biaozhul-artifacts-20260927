//! Exhaustive state-space exploration inside the finite capacity box.
//!
//! With every place carrying an explicit finite capacity the reachable set is
//! finite (at most `Π (capacity_p + 1)` markings), so reachability is decided
//! exactly by graph search. This is **not** the reachability problem for
//! unbounded Petri nets: an unbounded net has no finite capacity box and its
//! reachability problem is strictly harder (non-primitive recursive). A
//! capacity-bounded model must never be presented as a complete answer for an
//! unbounded one.

use std::collections::{HashMap, VecDeque};

use crate::fire::enabled_transitions;
use crate::{fire, Marking, Net, Token};

/// Search target. `Exact` matches one marking; `AnyOf` matches the first
/// reachable member of a set.
#[derive(Debug, Clone)]
pub enum Target {
    Exact(Marking),
    AnyOf(Vec<Marking>),
}

impl Target {
    pub fn matches(&self, m: &[Token]) -> bool {
        match self {
            Target::Exact(t) => t.as_slice() == m,
            Target::AnyOf(ts) => ts.iter().any(|t| t.as_slice() == m),
        }
    }
}

/// One firing in a witness path.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WitnessStep {
    /// Transition index in the net.
    pub transition: usize,
    pub transition_name: String,
    pub marking_before: Marking,
    pub marking_after: Marking,
}

/// Knobs bounding a single analysis run.
#[derive(Debug, Clone)]
pub struct ExploreConfig {
    /// Maximum number of distinct markings expanded. `None` means the full
    /// finite box.
    pub max_states: Option<usize>,
    /// Emit progress roughly every this many expansions.
    pub progress_every: usize,
}

impl Default for ExploreConfig {
    fn default() -> Self {
        ExploreConfig {
            max_states: None,
            progress_every: 10_000,
        }
    }
}

impl ExploreConfig {
    pub fn bounded(max_states: usize) -> Self {
        ExploreConfig {
            max_states: Some(max_states),
            ..Default::default()
        }
    }
}

/// Snapshot emitted while the search runs, for diagnostics.
#[derive(Debug, Clone)]
pub struct Progress {
    pub expanded: usize,
    pub discovered: usize,
    pub frontier: usize,
}

/// A reachable marking at which no transition is enabled.
#[derive(Debug, Clone)]
pub struct DeadlockInfo {
    pub marking: Marking,
    pub distance: usize,
}

/// Result categories. `Inconclusive` is a first-class outcome: an aborted or
/// truncated search must never be reported as unreachable.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Verdict {
    Reachable,
    Unreachable,
    Inconclusive,
}

/// Why the search stopped, separately from whether the target was reached.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StopReason {
    /// Whole reachable state space was explored.
    Exhausted,
    /// Configured state budget was spent before exhausting the space.
    StateLimit,
}

#[derive(Debug, Clone)]
pub struct ExplorationResult {
    pub verdict: Verdict,
    pub stop_reason: StopReason,
    /// Witness from the initial marking to the target, if reachable.
    pub path: Option<Vec<WitnessStep>>,
    pub stats: ExploreStats,
    /// All deadlock markings reachable from the initial marking.
    pub deadlocks: Vec<DeadlockInfo>,
}

#[derive(Debug, Clone)]
pub struct ExploreStats {
    pub expanded: usize,
    pub discovered: usize,
    pub deadlocks: usize,
    /// Size of the full finite capacity box, informational.
    pub capacity_box_size: Option<u128>,
}

struct Parent {
    /// Transition index used to arrive here from `parent`.
    transition: usize,
    parent: usize,
    distance: usize,
}

/// Run BFS from the net's initial marking. A progress callback may observe
/// counters; it cannot alter the search.
pub fn explore<F>(
    net: &Net,
    target: Option<&Target>,
    config: &ExploreConfig,
    mut on_progress: F,
) -> ExplorationResult
where
    F: FnMut(Progress),
{
    let capacity_box_size = capacity_box(net);

    let initial: Marking = net.initial().to_vec();
    let mut queue: VecDeque<usize> = VecDeque::new();
    // Marking -> node id. Node id is the marking's position in `states`.
    let mut state_id: HashMap<Marking, usize> = HashMap::new();
    let mut states: Vec<Marking> = Vec::new();
    let mut parents: Vec<Option<Parent>> = Vec::new();

    let root = 0usize;
    state_id.insert(initial.clone(), root);
    states.push(initial.clone());
    parents.push(None);
    queue.push_back(root);

    let mut target_id: Option<usize> = None;
    let mut deadlocks: Vec<DeadlockInfo> = Vec::new();
    let mut expanded = 0usize;
    let mut stop_reason = StopReason::Exhausted;

    // The initial marking could itself be the target / a deadlock.
    if target.is_some_and(|t| t.matches(&initial)) {
        target_id = Some(root);
    }

    while let Some(&id) = queue.front() {
        if target_id.is_some() {
            // Target found: stop expanding immediately (shortest BFS path).
            break;
        }
        if config
            .max_states
            .is_some_and(|limit| expanded >= limit)
        {
            stop_reason = StopReason::StateLimit;
            break;
        }
        queue.pop_front();
        let marking = states[id].clone();
        let enabled = enabled_transitions(net, &marking);
        expanded += 1;

        if enabled.is_empty() {
            let distance = parents[id].as_ref().map_or(0, |p| p.distance);
            deadlocks.push(DeadlockInfo {
                marking: marking.clone(),
                distance,
            });
        }

        for &t in &enabled {
            // `t` is enabled by construction; an error here is a kernel bug.
            let successor = fire(net, &marking, t).expect("enabled transition must fire");
            if state_id.contains_key(&successor) {
                continue;
            }
            let sid = states.len();
            state_id.insert(successor.clone(), sid);
            let distance = parents[id].as_ref().map_or(1, |p| p.distance + 1);
            parents.push(Some(Parent {
                transition: t,
                parent: id,
                distance,
            }));
            states.push(successor.clone());
            queue.push_back(sid);
            if target.is_some_and(|tg| tg.matches(&successor)) {
                target_id = Some(sid);
                break;
            }
        }

        if expanded % config.progress_every.max(1) == 0 {
            on_progress(Progress {
                expanded,
                discovered: states.len(),
                frontier: queue.len(),
            });
        }
    }

    let path = target_id.map(|tid| reconstruct_path(net, &states, &parents, tid));

    let verdict = match target_id {
        Some(_) => Verdict::Reachable,
        None => {
            if stop_reason == StopReason::Exhausted {
                Verdict::Unreachable
            } else {
                Verdict::Inconclusive
            }
        }
    };

    ExplorationResult {
        verdict,
        stop_reason,
        path,
        stats: ExploreStats {
            expanded,
            discovered: states.len(),
            deadlocks: deadlocks.len(),
            capacity_box_size,
        },
        deadlocks,
    }
}

fn reconstruct_path(
    net: &Net,
    states: &[Marking],
    parents: &[Option<Parent>],
    target_id: usize,
) -> Vec<WitnessStep> {
    let mut reversed: Vec<WitnessStep> = Vec::new();
    let mut cur = target_id;
    while let Some(p) = &parents[cur] {
        let before = states[p.parent].clone();
        let after = states[cur].clone();
        reversed.push(WitnessStep {
            transition: p.transition,
            transition_name: net.transition_name(p.transition).to_string(),
            marking_before: before,
            marking_after: after,
        });
        cur = p.parent;
    }
    reversed.reverse();
    reversed
}

/// Cardinality of the full capacity box, if it fits in u128.
pub fn capacity_box(net: &Net) -> Option<u128> {
    let mut size: u128 = 1;
    for p in 0..net.place_count() {
        size = size.checked_mul(net.capacity(p) as u128 + 1)?;
    }
    Some(size)
}

/// Convenience: exhaustive reachability with default bounds.
pub fn reachable(net: &Net, target: &Target) -> ExplorationResult {
    explore(net, Some(target), &ExploreConfig::default(), |_| {})
}

/// Convenience: enumerate the whole reachable space (also collects deadlocks).
pub fn whole_space(net: &Net, config: &ExploreConfig) -> ExplorationResult {
    explore(net, None, config, |_| {})
}

/// Token count of a marking; useful for invariants and tests.
pub fn total_tokens(m: &Marking) -> Token {
    m.iter().fold(0u64, |acc, &x| acc.saturating_add(x))
}
