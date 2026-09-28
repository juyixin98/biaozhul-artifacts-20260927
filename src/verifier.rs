//! Independent evidence verification.
//!
//! Everything here re-derives facts from the compiled models and never trusts
//! solver bookkeeping:
//!
//! * [`verify_replay`] walks every concrete edge of a claimed replay, checking
//!   source, action and target, and confirms the run ends in an accepting
//!   state and spells the claimed observable word;
//! * [`trace_accepted`] independently answers "does this LTS weakly accept
//!   this observable word?" with its own closure + DFS implementation;
//! * [`verify_counterexample`] combines both sides: the implementation replay
//!   must be valid and accepting while the specification must *not* accept the
//!   same word — the observable acceptance difference the verdict rests on.

use std::collections::HashSet;

use serde::Serialize;

use crate::compiler::SILENT;
use crate::error::{EngineError, EngineResult};
use crate::model::{Pair, StateId};
use crate::witness::Replay;

#[derive(Debug, Clone, Serialize)]
pub struct Verification {
    pub implementation_run_valid: bool,
    pub specification_accepts_trace: bool,
    pub implementation_accepts_trace: bool,
    pub confirmed: bool,
    pub problems: Vec<String>,
}

/// Walk one phase of (claimed) silent edges, mutating `cur` to the declared
/// target as the chain is followed. Every inconsistency (wrong source state,
/// missing edge, wrong label/action/target) is recorded; the walk keeps
/// following the *declared* chain rather than stopping, so a problem early in
/// the replay does not hide independent problems later. A false return means
/// at least one edge in this phase was invalid.
fn walk_tau_phase(
    pair: &Pair,
    lts: &crate::model::Lts,
    edges: &[crate::witness::EdgeRefDto],
    cur: &mut StateId,
    problems: &mut Vec<String>,
    phase: &str,
) {
    for e in edges {
        let Some(&src) = lts.state_index().get(e.state.as_str()) else {
            problems.push(format!("{phase}: unknown state '{}'", e.state));
            continue;
        };
        if src != *cur {
            problems.push(format!(
                "{phase}: edge declared from '{}' but current state is '{}'",
                e.state,
                lts.name_of_state(*cur)
            ));
        }
        let Some(edge) = lts
            .outgoing
            .get(src as usize)
            .and_then(|v| v.get(e.edge_id as usize))
        else {
            problems.push(format!(
                "{phase}: edge_id {} does not exist in state '{}'",
                e.edge_id, e.state
            ));
            continue;
        };
        if edge.label != SILENT {
            problems.push(format!(
                "{phase}: edge {}#{} is observable ('{}'), expected a silent step",
                e.state,
                e.edge_id,
                pair.label_name(edge.label)
            ));
        }
        if e.action != pair.silent_name {
            problems.push(format!(
                "{phase}: edge action annotated '{}', expected silent '{}'",
                e.action, pair.silent_name
            ));
        }
        let target_name = lts.name_of_state(edge.target);
        if target_name != e.target {
            problems.push(format!(
                "{phase}: edge actually targets '{target_name}', replay says '{}'",
                e.target
            ));
        }
        // Track the declared edge regardless of prior mismatches.
        *cur = edge.target;
    }
}

/// Check every edge reference of a replay against the raw transition table.
/// Returns (valid, observable actions actually spelled, detailed problems).
pub fn verify_replay(pair: &Pair, replay: &Replay) -> EngineResult<(bool, Vec<String>, Vec<String>)> {
    let lts = &pair.impl_;
    let mut problems = Vec::new();
    let mut spelled: Vec<String> = Vec::new();

    let mut cur = match lts.state_index().get(replay.start_state.as_str()) {
        Some(&s) => s,
        None => {
            return Err(EngineError::input(
                "verifier_unknown_state",
                format!(
                    "{}: replay start state '{}' does not exist",
                    lts.name, replay.start_state
                ),
            ));
        }
    };
    if cur != lts.initial {
        problems.push(format!(
            "replay starts at '{}' but implementation initial state is '{}'",
            replay.start_state,
            lts.name_of_state(lts.initial)
        ));
    }

    let resolve = |name: &str| -> Option<StateId> { lts.state_index().get(name).copied() };

    for (i, hop) in replay.hops.iter().enumerate() {
        walk_tau_phase(
            pair,
            lts,
            &hop.before_tau,
            &mut cur,
            &mut problems,
            &format!("hop {i} pre-tau"),
        );

        // Observable edge. Every check is recorded independently; when the
        // referenced edge exists we still follow its declared target so the
        // rest of the replay can be validated.
        let mut hop_valid = true;
        let src = match resolve(&hop.observable_edge.state) {
            Some(s) => s,
            None => {
                problems.push(format!(
                    "hop {i}: unknown state '{}'",
                    hop.observable_edge.state
                ));
                continue;
            }
        };
        if src != cur {
            problems.push(format!(
                "hop {i}: observable edge declared from '{}' but current state is '{}'",
                hop.observable_edge.state,
                lts.name_of_state(cur)
            ));
        }
        let Some(edge) = lts
            .outgoing
            .get(src as usize)
            .and_then(|v| v.get(hop.observable_edge.edge_id as usize))
            .copied()
        else {
            problems.push(format!(
                "hop {i}: edge_id {} does not exist in state '{}'",
                hop.observable_edge.edge_id, hop.observable_edge.state
            ));
            continue;
        };
        if edge.label == SILENT {
            problems.push(format!("hop {i}: observable edge is actually silent"));
            hop_valid = false;
        }
        let actual_action = if edge.label == SILENT {
            pair.silent_name.clone()
        } else {
            pair.label_name(edge.label).to_string()
        };
        if actual_action != hop.action {
            problems.push(format!(
                "hop {i}: replay claims action '{}' but edge is labeled '{actual_action}'",
                hop.action
            ));
            hop_valid = false;
        }
        let target_name = lts.name_of_state(edge.target);
        if target_name != hop.observable_edge.target {
            problems.push(format!(
                "hop {i}: edge targets '{target_name}', replay says '{}'",
                hop.observable_edge.target
            ));
            hop_valid = false;
        }
        if hop_valid {
            spelled.push(actual_action);
        }
        cur = edge.target;

        walk_tau_phase(
            pair,
            lts,
            &hop.after_tau,
            &mut cur,
            &mut problems,
            &format!("hop {i} post-tau"),
        );
    }

    walk_tau_phase(pair, lts, &replay.final_tau, &mut cur, &mut problems, "final-tau");

    if lts.name_of_state(cur) != replay.accepting_state {
        problems.push(format!(
            "replay ends in '{}' but accepting_state says '{}'",
            lts.name_of_state(cur),
            replay.accepting_state
        ));
    }
    if !lts.accepting[cur as usize] {
        problems.push(format!(
            "replay ends in non-accepting state '{}'",
            replay.accepting_state
        ));
    }

    let valid = problems.is_empty();
    Ok((valid, spelled, problems))
}

/// Independent weak-acceptance test with its own closure computation and DFS.
/// `word` is given as observable action names.
pub fn trace_accepted(pair: &Pair, spec_side: bool, word: &[String]) -> EngineResult<bool> {
    let lts = if spec_side {
        &pair.spec
    } else {
        &pair.impl_
    };

    // Resolve action names against the aligned alphabet. An action outside
    // the alphabet cannot label any edge of this LTS, so such a word is
    // simply not accepted.
    let mut labels: Vec<crate::model::LabelId> = Vec::with_capacity(word.len());
    for a in word {
        let Some(id) = pair.label_names.iter().position(|n| n == a).map(|i| i as crate::model::LabelId)
        else {
            return Ok(false);
        };
        labels.push(id);
    }

    // Closure as a simple fixpoint per state.
    let closure_of = |s: StateId| -> Vec<StateId> {
        let mut seen = HashSet::new();
        let mut stack = vec![s];
        seen.insert(s);
        while let Some(q) = stack.pop() {
            for e in &lts.outgoing[q as usize] {
                if e.label == SILENT && seen.insert(e.target) {
                    stack.push(e.target);
                }
            }
        }
        let mut v: Vec<StateId> = seen.into_iter().collect();
        v.sort_unstable();
        v
    };

    let mut current: Vec<StateId> = closure_of(lts.initial);
    for a in labels {
        let mut next = HashSet::new();
        for q in &current {
            for e in &lts.outgoing[*q as usize] {
                if e.label == a {
                    for t in closure_of(e.target) {
                        next.insert(t);
                    }
                }
            }
        }
        current = next.into_iter().collect();
        current.sort_unstable();
        if current.is_empty() {
            return Ok(false);
        }
    }
    Ok(current.iter().any(|&s| lts.accepting[s as usize]))
}

/// Full independent cross-check of a counterexample.
pub fn verify_counterexample(
    pair: &Pair,
    replay: &Replay,
    claimed_trace: &[String],
) -> EngineResult<Verification> {
    let (run_valid, spelled, replay_problems) = verify_replay(pair, replay)?;
    let impl_accepts = trace_accepted(pair, false, claimed_trace)?;
    let spec_accepts = trace_accepted(pair, true, claimed_trace)?;

    let mut problems = replay_problems;
    if !run_valid && !problems.iter().any(|p| p.contains("did not validate")) {
        problems.insert(0, "implementation replay did not validate".to_string());
    }
    if spelled != claimed_trace {
        problems.push(format!(
            "replay spells {spelled:?} but counterexample trace claims {claimed_trace:?}"
        ));
    }
    if !impl_accepts {
        problems.push("independent oracle says implementation does NOT accept the trace".into());
    }
    if spec_accepts {
        problems.push("independent oracle says specification DOES accept the trace".into());
    }

    Ok(Verification {
        implementation_run_valid: run_valid,
        specification_accepts_trace: spec_accepts,
        implementation_accepts_trace: impl_accepts,
        confirmed: problems.is_empty(),
        problems,
    })
}
