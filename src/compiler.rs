//! Compile the JSON input language into the dense [`Pair`] representation.
//!
//! Responsibilities:
//! * name validation (empty names and duplicate declarations are rejected);
//! * size limits (refuse pathological inputs before any search);
//! * reference resolution (unknown states are a state conflict);
//! * explicit alphabet alignment between the two LTSs;
//! * label interning shared by both sides.

use std::collections::BTreeMap;

use crate::error::{EngineError, EngineResult};
use crate::input::{CheckRequest, LtsDef};
use crate::model::{Edge, LabelId, Lts, Pair, StateId};

#[derive(Debug)]
struct CompiledNames {
    state_names: Vec<String>,
    index: BTreeMap<String, StateId>,
}

impl CompiledNames {
    fn lookup(&self, name: &str, side: &str) -> EngineResult<StateId> {
        self.index.get(name).copied().ok_or_else(|| {
            EngineError::conflict(
                "unknown_state",
                format!("{side} references state '{name}', which is not declared"),
            )
        })
    }
}

/// Intern one side's state names. An explicit `states` list must be non-empty
/// and contain no duplicates; transition endpoints are validated against it.
fn intern_states(def: &LtsDef, max_states: usize) -> EngineResult<CompiledNames> {
    let side = format!("{} (LTS side)", def.name);
    let mut names: Vec<String> = Vec::new();
    let mut index: BTreeMap<String, StateId> = BTreeMap::new();
    let mut intern = |n: &str| -> EngineResult<()> {
        if n.is_empty() {
            return Err(EngineError::input(
                "empty_state_name",
                format!("{side}: empty state name"),
            ));
        }
        if !index.contains_key(n) {
            let id = names.len() as StateId;
            names.push(n.to_string());
            index.insert(n.to_string(), id);
        }
        Ok(())
    };

    if def.states.is_empty() {
        // Implicit state universe: initial + every transition endpoint, deduped.
        intern(&def.initial)?;
        for t in &def.transitions {
            intern(&t.from)?;
            intern(&t.to)?;
        }
    } else {
        // Explicit state universe: every declaration counts, duplicates are a
        // conflict (they would make accepting-state lists ambiguous).
        for n in &def.states {
            if n.is_empty() {
                return Err(EngineError::input(
                    "empty_state_name",
                    format!("{side}: empty state name in states list"),
                ));
            }
            if index.contains_key(n) {
                return Err(EngineError::conflict(
                    "duplicate_state",
                    format!("{side}: state '{n}' declared more than once"),
                ));
            }
            let id = names.len() as StateId;
            names.push(n.clone());
            index.insert(n.clone(), id);
        }
        let need = |n: &str, where_: &str| -> EngineResult<()> {
            if !index.contains_key(n) {
                Err(EngineError::conflict(
                    "unknown_state",
                    format!("{side}: {where_} references state '{n}', which is not in states"),
                ))
            } else {
                Ok(())
            }
        };
        need(&def.initial, "initial state")?;
        for t in &def.transitions {
            need(&t.from, "transition source")?;
            need(&t.to, "transition target")?;
        }
    }

    if names.len() > max_states {
        return Err(EngineError::exhausted(
            "too_many_states",
            format!("{side}: {} states exceeds limit {max_states}", names.len()),
        ));
    }
    Ok(CompiledNames {
        state_names: names,
        index,
    })
}

fn validate_actions(def: &LtsDef, silent: &str) -> EngineResult<()> {
    if silent.is_empty() {
        return Err(EngineError::input(
            "empty_silent_action",
            format!("{}: silent action name must not be empty", def.name),
        ));
    }
    for t in &def.transitions {
        if t.action.is_empty() {
            return Err(EngineError::input(
                "empty_action",
                format!(
                    "{}: transition {} -> {} has an empty action",
                    def.name, t.from, t.to
                ),
            ));
        }
    }
    Ok(())
}

fn compile_side(
    def: &LtsDef,
    names: &CompiledNames,
    label_index: &BTreeMap<String, LabelId>,
    silent: &str,
    label_count: usize,
    max_transitions: usize,
) -> EngineResult<Lts> {
    let n = names.state_names.len();
    let mut outgoing = vec![Vec::new(); n];
    let mut accepting = vec![false; n];

    match &def.accepting {
        None => {
            for a in accepting.iter_mut() {
                *a = true;
            }
        }
        Some(list) => {
            for s in list {
                let id = names.lookup(s, &def.name)?;
                accepting[id as usize] = true;
            }
        }
    }

    if def.transitions.len() > max_transitions {
        return Err(EngineError::exhausted(
            "too_many_transitions",
            format!(
                "{}: {} transitions exceeds limit {max_transitions}",
                def.name,
                def.transitions.len()
            ),
        ));
    }

    for t in &def.transitions {
        let from = names.lookup(&t.from, &def.name)?;
        let to = names.lookup(&t.to, &def.name)?;
        if t.action == silent {
            // Silent steps are encoded with `LabelId::MAX` and filtered out
            // wherever labels are iterated.
            outgoing[from as usize].push(Edge {
                label: SILENT,
                target: to,
            });
        } else {
            let label = label_index.get(&t.action).copied().ok_or_else(|| {
                EngineError::input(
                    "action_not_in_alphabet",
                    format!(
                        "{}: transition uses action '{}' which is not in the aligned alphabet",
                        def.name, t.action
                    ),
                )
            })?;
            debug_assert!((label as usize) < label_count);
            outgoing[from as usize].push(Edge { label, target: to });
        }
    }

    let initial = names.lookup(&def.initial, &def.name)?;
    Ok(Lts {
        name: def.name.clone(),
        state_names: names.state_names.clone(),
        initial,
        outgoing,
        accepting,
        label_count,
    })
}

/// Sentinel label id for internal (silent) steps.
pub const SILENT: LabelId = LabelId::MAX;

/// Compile a full request. Returns the aligned [`Pair`] plus the resolved
/// alphabet in declaration order (explicit) or sorted union order (derived).
pub fn compile(req: &CheckRequest) -> EngineResult<Pair> {
    let limits = req.limits();
    let silent = req.silent_action.clone();

    validate_actions(&req.specification, &silent)?;
    validate_actions(&req.implementation, &silent)?;

    // Collect / validate the observable alphabet. Either form is sorted
    // afterwards so LabelIds are deterministic regardless of input order.
    let alphabet: Vec<String> = match &req.alphabet {
        Some(a) => {
            if a.is_empty() {
                return Err(EngineError::input(
                    "empty_alphabet",
                    "explicit alphabet must not be empty; omit it to derive the union",
                ));
            }
            let mut seen: BTreeMap<&str, ()> = BTreeMap::new();
            for action in a {
                if action.is_empty() {
                    return Err(EngineError::input(
                        "empty_action",
                        "alphabet contains an empty action name",
                    ));
                }
                if action == &silent {
                    return Err(EngineError::conflict(
                        "silent_also_observable",
                        format!("action '{silent}' is declared both silent and observable"),
                    ));
                }
                if seen.insert(action.as_str(), ()).is_some() {
                    return Err(EngineError::conflict(
                        "duplicate_alphabet_action",
                        format!("alphabet declares action '{action}' more than once"),
                    ));
                }
            }
            let mut sorted = a.clone();
            sorted.sort();
            sorted
        }
        None => {
            let mut set: BTreeMap<String, ()> = BTreeMap::new();
            for def in [&req.specification, &req.implementation] {
                for t in &def.transitions {
                    if t.action != silent {
                        set.insert(t.action.clone(), ());
                    }
                }
            }
            set.into_keys().collect()
        }
    };

    let label_index: BTreeMap<String, LabelId> = alphabet
        .iter()
        .enumerate()
        .map(|(i, a)| (a.clone(), i as LabelId))
        .collect();

    let spec_names = intern_states(&req.specification, limits.max_states_per_lts)?;
    let impl_names = intern_states(&req.implementation, limits.max_states_per_lts)?;

    let spec = compile_side(
        &req.specification,
        &spec_names,
        &label_index,
        &silent,
        alphabet.len(),
        limits.max_transitions_per_lts,
    )?;
    let impl_ = compile_side(
        &req.implementation,
        &impl_names,
        &label_index,
        &silent,
        alphabet.len(),
        limits.max_transitions_per_lts,
    )?;

    Ok(Pair {
        spec,
        impl_,
        label_names: alphabet,
        silent_name: silent,
    })
}
