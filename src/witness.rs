//! Replay witness construction.
//!
//! The solver kernel works with *sets* of states; a counterexample is, on its
//! own, just a list of action names. This module turns that list into a fully
//! concrete run of the implementation: for each observable action it records
//! the silent steps before it, the observable edge itself, and the silent
//! steps after it, ending on an accepting state.
//!
//! It is deliberately independent of the solver's bookkeeping: it only trusts
//! [`crate::closure::ClosureTable`] reachability and the compiled LTS edges,
//! and re-derives the path with its own depth-first search. The output is
//! later re-checked again by the verifier module, which shares no search code.

use serde::{Deserialize, Serialize};

use crate::closure::ClosureTable;
use crate::error::{EngineError, EngineResult};
use crate::model::{EdgeRef, LabelId, Lts, StateId};

/// One observable step of a replayed weak trace.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Hop {
    /// Silent steps performed before the observable action.
    pub before_tau: Vec<EdgeRefDto>,
    pub action: String,
    pub observable_edge: EdgeRefDto,
    /// Silent steps performed after the observable action.
    pub after_tau: Vec<EdgeRefDto>,
}

/// JSON-friendly edge reference: edge id is local to `state`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EdgeRefDto {
    pub state: String,
    pub edge_id: u32,
    pub action: String,
    pub target: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Replay {
    pub side: String,
    pub start_state: String,
    pub hops: Vec<Hop>,
    /// Trailing silent steps into the accepting state.
    pub final_tau: Vec<EdgeRefDto>,
    pub accepting_state: String,
    /// Total concrete edges (silent + observable) in the replay.
    pub edge_count: usize,
}

struct Resolver<'a> {
    silent_name: &'a str,
    label_name: &'a dyn Fn(LabelId) -> String,
}

fn edge_dto_resolved(lts: &Lts, r: EdgeRef, res: &Resolver) -> Option<EdgeRefDto> {
    let (label, target) = lts.edge(r)?;
    Some(EdgeRefDto {
        state: lts.name_of_state(r.source).to_string(),
        edge_id: r.edge_id,
        action: if label == crate::compiler::SILENT {
            res.silent_name.to_string()
        } else {
            (res.label_name)(label)
        },
        target: lts.name_of_state(target).to_string(),
    })
}

/// Concrete accepting run for `word` in `lts`, or `None` if none exists within
/// the edge budget. Deterministic: tau paths are shortest BFS paths and edges
/// are tried in declaration order.
pub fn build_accepting_run(
    lts: &Lts,
    closures: &mut ClosureTable<'_>,
    word: &[LabelId],
    silent_name: &str,
    label_name: &dyn Fn(LabelId) -> String,
    edge_budget: usize,
) -> EngineResult<Option<Replay>> {
    let res = Resolver {
        silent_name,
        label_name,
    };

    // Search state: which observable hop we are on and the concrete state.
    // A "hop plan" records, per phase, the tau path + observable edge taken.
    struct Choice {
        // tau path into the source of the observable edge
        before: Vec<EdgeRef>,
        obs: EdgeRef,
        // tau path after the observable edge (always shortest for the chosen
        // intermediate target; fixed once obs is chosen)
        after: Vec<EdgeRef>,
        // state reached after `after`
        landed: StateId,
    }

    fn dfs(
        lts: &Lts,
        closures: &mut ClosureTable<'_>,
        word: &[LabelId],
        depth: usize,
        cur: StateId,
        choices: &mut Vec<Choice>,
        used_edges: usize,
        budget: usize,
    ) -> EngineResult<bool> {
        if depth == word.len() {
            // The word is fully matched; this branch only counts if some
            // accepting state is reachable from the resting state by zero or
            // more trailing silent steps. (Checking it here rather than after
            // the DFS lets the search backtrack past non-accepting landings
            // — e.g. an early observable edge that reaches a dead end while a
            // tau detour would have reached an accepting state.)
            return Ok(closures
                .of(cur)
                .iter()
                .any(|&t| lts.accepting[t as usize]));
        }
        let action = word[depth];
        // Candidate observable-edge sources: every state in the tau-closure of
        // `cur`, in closure (sorted) order.
        let sources: Vec<StateId> = closures.of(cur).to_vec();
        for q in sources {
            let before = closures
                .tau_path(cur, q)
                .ok_or_else(|| EngineError::internal("closure_inconsistent", "tau_path failed"))?;
            if used_edges + before.len() + 1 > budget {
                continue;
            }
            for (edge_id, e) in lts.outgoing[q as usize]
                .iter()
                .enumerate()
                .filter(|(_, e)| e.label == action)
            {
                let obs = EdgeRef {
                    source: q,
                    edge_id: edge_id as u32,
                };
                // Possible resting states after the observable edge: the edge
                // target plus everything reachable from it by tau steps.
                let landings: Vec<StateId> = closures.of(e.target).to_vec();
                for landed in landings {
                    let after = closures.tau_path(e.target, landed).ok_or_else(|| {
                        EngineError::internal("closure_inconsistent", "tau_path failed")
                    })?;
                    let total = used_edges + before.len() + 1 + after.len();
                    if total > budget {
                        continue;
                    }
                    choices.push(Choice {
                        before: before.clone(),
                        obs,
                        after: after.clone(),
                        landed,
                    });
                    if dfs(lts, closures, word, depth + 1, landed, choices, total, budget)? {
                        return Ok(true);
                    }
                    choices.pop();
                }
            }
        }
        Ok(false)
    }

    let start = lts.initial;
    let mut choices: Vec<Choice> = Vec::new();
    let found = dfs(
        lts,
        closures,
        word,
        0,
        start,
        &mut choices,
        0,
        edge_budget,
    )?;
    if !found {
        return Ok(None);
    }

    // Trailing tau phase into an accepting state.
    let last = choices.last().map(|c| c.landed).unwrap_or(start);
    let target = closures
        .of(last)
        .iter()
        .copied()
        .find(|t| lts.accepting[*t as usize]);
    let Some(target) = target else {
        // Word can be matched but cannot be extended to an accepting state —
        // should not happen when called on a real counterexample.
        return Ok(None);
    };
    let final_tau = closures
        .tau_path(last, target)
        .ok_or_else(|| EngineError::internal("closure_inconsistent", "tau_path failed"))?;

    let mut hops = Vec::new();
    let mut edge_count = 0usize;
    for c in &choices {
        edge_count += c.before.len() + 1 + c.after.len();
        hops.push(Hop {
            before_tau: c
                .before
                .iter()
                .map(|r| edge_dto_resolved(lts, *r, &res))
                .collect::<Option<Vec<_>>>()
                .ok_or_else(|| EngineError::internal("bad_edge_ref", "invalid tau edge ref"))?,
            action: (res.label_name)(word[hops.len()]),
            observable_edge: edge_dto_resolved(lts, c.obs, &res)
                .ok_or_else(|| EngineError::internal("bad_edge_ref", "invalid observable edge"))?,
            after_tau: c
                .after
                .iter()
                .map(|r| edge_dto_resolved(lts, *r, &res))
                .collect::<Option<Vec<_>>>()
                .ok_or_else(|| EngineError::internal("bad_edge_ref", "invalid tau edge ref"))?,
        });
    }
    edge_count += final_tau.len();
    if edge_count > edge_budget {
        return Err(EngineError::exhausted(
            "witness_too_long",
            format!("witness uses {edge_count} edges, over budget {edge_budget}"),
        ));
    }

    Ok(Some(Replay {
        side: lts.name.clone(),
        start_state: lts.name_of_state(start).to_string(),
        hops,
        final_tau: final_tau
            .iter()
            .map(|r| edge_dto_resolved(lts, *r, &res))
            .collect::<Option<Vec<_>>>()
            .ok_or_else(|| EngineError::internal("bad_edge_ref", "invalid final tau edge"))?,
        accepting_state: lts.name_of_state(target).to_string(),
        edge_count,
    }))
}
