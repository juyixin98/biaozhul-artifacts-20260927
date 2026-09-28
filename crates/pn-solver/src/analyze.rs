//! End-to-end analysis orchestration: bounded search per target, witness
//! extraction and invariant-based necessary-condition checks.

use pn_core::explore::{
    capacity_box, explore, ExploreConfig, Target, Verdict as KernelVerdict,
    WitnessStep,
};
use pn_core::{Marking, Net};
use pn_lang::AnalysisInput;

use crate::incidence::incidence_matrix;
use crate::invariant::{farkas_p_invariants, InvariantCandidate};

/// One atomic firing in a witness path, serialisable names and markings.
#[derive(Debug, Clone)]
pub struct WitnessFire {
    pub step: usize,
    pub transition: String,
    pub before: Marking,
    pub after: Marking,
}

/// Reachability result for a single requested target.
#[derive(Debug, Clone)]
pub struct TargetOutcome {
    pub label: Option<String>,
    pub target: Marking,
    pub reachable: bool,
    /// One of `REACHABLE`, `UNREACHABLE`, `INCONCLUSIVE`.
    pub verdict: String,
    /// Why the search stopped (`EXHAUSTED` or `STATE_LIMIT`).
    pub stop_reason: String,
    pub path: Option<Vec<WitnessFire>>,
    pub distance: Option<usize>,
    pub states_expanded: usize,
    pub states_discovered: usize,
}

/// A reachable deadlock marking.
#[derive(Debug, Clone)]
pub struct DeadlockMarking {
    pub marking: Marking,
    pub distance: usize,
}

/// A P-invariant candidate with its necessary-condition check against a target.
#[derive(Debug, Clone)]
pub struct InvariantCheck {
    pub weights: Vec<i128>,
    pub support: Vec<usize>,
    pub support_minimal_in_set: bool,
    /// Weighted sum at the initial marking.
    pub at_initial: i128,
    /// Weighted sum at the target marking.
    pub at_target: i128,
    /// A P-invariant must give equal weighted sums for *reachable* markings;
    /// inequality is a sound certificate of unreachability.
    pub preserves_target: bool,
}

/// Explicit modelling-scope statement echoed in every response.
#[derive(Debug, Clone)]
pub struct ScopeNotice {
    pub model: String,
    pub equivalent_to_unbounded_decision: bool,
    pub explanation: String,
}

/// Complete result for one analysis request.
#[derive(Debug, Clone)]
pub struct AnalysisOutcome {
    pub net_name: String,
    pub place_names: Vec<String>,
    pub transition_names: Vec<String>,
    pub initial: Marking,
    pub capacity_box_size: Option<u128>,
    pub targets: Vec<TargetOutcome>,
    pub deadlocks: Vec<DeadlockMarking>,
    pub invariants: Vec<InvariantCandidate>,
    /// `invariants[i]` check per target (`targets` order); empty when no targets.
    pub invariant_checks: Vec<Vec<InvariantCheck>>,
    pub invariant_generation_truncated: bool,
    /// True when deadlock enumeration stopped at the state budget; the
    /// reported deadlock list is then partial and must not be read as
    /// exhaustive.
    pub deadlocks_truncated: bool,
    /// Total expansion budget behaviour across the whole run.
    pub total_states_expanded: usize,
    pub scope: ScopeNotice,
}

/// Run every requested analysis on validated input.
pub fn analyze(input: &AnalysisInput) -> AnalysisOutcome {
    analyze_with_config(input, None)
}

/// `extra_state_budget` overrides the per-target state limit when supplied.
pub fn analyze_with_config(
    input: &AnalysisInput,
    extra_state_budget: Option<usize>,
) -> AnalysisOutcome {
    let net: &Net = &input.net;
    let config = ExploreConfig {
        max_states: extra_state_budget.or(input.options.max_states),
        progress_every: 10_000,
    };

    let capacity_box_size = capacity_box(net);

    // Per-target bounded search.
    let mut target_outcomes = Vec::with_capacity(input.targets.len());
    let mut total_expanded = 0usize;
    for (i, target_marking) in input.targets.iter().enumerate() {
        let target = Target::Exact(target_marking.clone());
        let result = explore(net, Some(&target), &config, |_| {});
        total_expanded += result.stats.expanded;

        let path = result.path.map(to_witness);
        let distance = path.as_ref().map(|p| p.len());
        let (verdict, stop_reason, reachable) = match result.verdict {
            KernelVerdict::Reachable => ("REACHABLE", stop_name(&result.stop_reason), true),
            KernelVerdict::Unreachable => ("UNREACHABLE", stop_name(&result.stop_reason), false),
            KernelVerdict::Inconclusive => {
                ("INCONCLUSIVE", stop_name(&result.stop_reason), false)
            }
        };

        target_outcomes.push(TargetOutcome {
            label: input.target_labels.get(i).cloned().flatten(),
            target: target_marking.clone(),
            reachable,
            verdict: verdict.to_string(),
            stop_reason: stop_reason.to_string(),
            path,
            distance,
            states_expanded: result.stats.expanded,
            states_discovered: result.stats.discovered,
        });
    }

    // Whole-space enumeration for deadlocks (shares no cache with the target
    // searches; correctness over micro-optimisation at these sizes).
    let (deadlocks, deadlocks_truncated) = if input.options.find_deadlocks {
        let space = explore(net, None, &config, |_| {});
        total_expanded += space.stats.expanded;
        let truncated = space.stop_reason == pn_core::explore::StopReason::StateLimit;
        (
            space
                .deadlocks
                .into_iter()
                .map(|d| DeadlockMarking {
                    marking: d.marking,
                    distance: d.distance,
                })
                .collect(),
            truncated,
        )
    } else {
        (Vec::new(), false)
    };

    // P-invariants and per-target necessary-condition checks.
    let mut invariants = Vec::new();
    let mut invariant_checks = Vec::new();
    let mut truncated = false;
    if input.options.compute_invariants {
        let c = incidence_matrix(net);
        let fres = farkas_p_invariants(&c);
        truncated = fres.truncated;
        invariants = fres.candidates;
        for target_marking in &input.targets {
            let checks = invariants
                .iter()
                .map(|inv| {
                    let at_initial = c.weighted_sum(&inv.weights, net.initial());
                    let at_target = c.weighted_sum(&inv.weights, target_marking);
                    InvariantCheck {
                        weights: inv.weights.clone(),
                        support: inv.support.clone(),
                        support_minimal_in_set: inv.support_minimal_in_set,
                        at_initial,
                        at_target,
                        preserves_target: at_initial == at_target,
                    }
                })
                .collect();
            invariant_checks.push(checks);
        }
    }

    AnalysisOutcome {
        net_name: input.name.clone(),
        place_names: (0..net.place_count())
            .map(|i| net.place_name(i).to_string())
            .collect(),
        transition_names: (0..net.transition_count())
            .map(|i| net.transition_name(i).to_string())
            .collect(),
        initial: net.initial().to_vec(),
        capacity_box_size,
        targets: target_outcomes,
        deadlocks,
        invariants,
        invariant_checks,
        invariant_generation_truncated: truncated,
        deadlocks_truncated,
        total_states_expanded: total_expanded,
        scope: ScopeNotice {
            model: "bounded-capacity ordinary weighted Petri net".into(),
            equivalent_to_unbounded_decision: false,
            explanation: concat!(
                "Reachability is decided by exhaustive search inside the finite ",
                "product of per-place capacities. This is exact FOR THAT BOUNDED MODEL ",
                "but is not a complete reachability decision for an unbounded Petri net: ",
                "removing a capacity admits markings outside this box and changes the ",
                "answer.",
            )
            .into(),
        },
    }
}

fn to_witness(steps: Vec<WitnessStep>) -> Vec<WitnessFire> {
    steps
        .into_iter()
        .enumerate()
        .map(|(i, s)| WitnessFire {
            step: i + 1,
            transition: s.transition_name,
            before: s.marking_before,
            after: s.marking_after,
        })
        .collect()
}

fn stop_name(s: &pn_core::explore::StopReason) -> &'static str {
    match s {
        pn_core::explore::StopReason::Exhausted => "EXHAUSTED",
        pn_core::explore::StopReason::StateLimit => "STATE_LIMIT",
    }
}
