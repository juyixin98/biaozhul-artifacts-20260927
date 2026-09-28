//! Subset-minimal unsatisfiable core (MUS) extraction.
//!
//! Terminology, kept strictly separate on purpose:
//!
//! * **Subset-minimal / irreducible** core C: C is UNSAT and removing *any single*
//!   member makes it SAT. That is what this service produces and certifies.
//! * **Cardinality-minimum** core: the smallest possible core by size. We do NOT
//!   optimize for that, and outputs are never labelled "minimum".
//!
//! `find_one` returns one certified core. `find_all` uses **core packing**: after a
//! core is found its whole member set is removed before the next round, so the
//! emitted cores are mutually disjoint. That is a diversity heuristic, NOT MUS
//! enumeration — a maximum packing is not guaranteed and MUSes overlapping an
//! already-packed core are not rediscovered. Every emitted core is nonetheless
//! independently certified subset-minimal.
//!
//! The algorithm is classic **deletion-based** extraction:
//!
//! 1. Probe the whole formula. SAT ⇒ no core. UNKNOWN ⇒ no conclusion.
//! 2. Walk constraints in a fixed order; tentatively delete each one. If the
//!    remainder is still UNSAT the deletion is permanent; if it is SAT the
//!    constraint is indispensable and is put back, and the SAT witness is recorded.
//! 3. After the walk, a minimality pass re-derives a fresh SAT witness for
//!    `C \\ {m}` for *every* member m of the surviving set C. Only if all of those
//!    are SAT (and C itself UNSAT) is C labelled [`CoreVerdict::CertifiedMus`].
//!
//! Every decision lands in [`TraceEntry`] with the exact tried id-set and the
//! solver verdict, so a third party can re-run and audit each deletion.
//!
//! `UNKNOWN` is a terminal obstruction, never an UNSAT. On cancellation or budget
//! exhaustion the currently verified UNSAT candidate and all proof state gathered
//! so far are preserved (not thrown away).

use crate::language::{Cnf, Model};
use crate::solver::{CancelToken, SStatus, SolveCtx, Solver, Budget};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum ExtractMode {
    /// Stop after the first certified core.
    #[default]
    FindOne,
    /// Return a sequence of mutually *disjoint* cores via core packing (a diversity
    /// strategy — see module docs; not exhaustive MUS enumeration).
    FindAll,
}

/// Certification level of an emitted core.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CoreVerdict {
    /// Core UNSAT, and a fresh SAT witness for `core \\ {m}` exists for every member.
    CertifiedMus,
    /// An UNSAT candidate was found but some minimality witness could not be
    /// obtained (UNKNOWN / budget / cancel), so minimality is not certified.
    UncertifiedUnsatCandidate,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    /// Probe of the whole current formula.
    FullCheck,
    /// Tentative single-constraint deletion test.
    Deletion,
    /// Final per-member minimality witness pass.
    MinimalityPass,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Termination {
    /// Algorithm finished; reported cores are certified.
    Completed,
    /// Input formula (or remaining formula) is satisfiable — no (further) core.
    Satisfiable,
    /// Budget ran out mid-way; `retained_candidate` is still verified UNSAT.
    BudgetExhausted,
    /// Client cancelled; verified candidate and proof state are retained.
    Cancelled,
    /// A solver returned UNKNOWN for a decisive query (budget/cancel unrelated).
    SolverUnknown,
}

/// One auditable solver decision.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TraceEntry {
    pub seq: u64,
    pub round: usize,
    pub phase: Phase,
    /// Constraint whose deletion is being tested (absent on the full-formula probe).
    pub tested_id: Option<String>,
    /// Exact id-set handed to the solver for this decision.
    pub trial_ids: Vec<String>,
    pub verdict: String,
    /// Whether `tested_id` ended up kept after this decision.
    pub kept: Option<bool>,
    pub solver: String,
    pub budget_used_after: u64,
    pub detail: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FoundCore {
    pub member_ids: Vec<String>,
    pub size: usize,
    pub verdict: CoreVerdict,
    /// For each member m: a model satisfying `core \\ {m}` — the minimality proofs.
    /// (Empty slots are exactly why a core is `UncertifiedUnsatCandidate`.)
    #[serde(skip_serializing_if = "BTreeMap::is_empty", default)]
    pub minimality_witnesses: BTreeMap<String, Model>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExtractionReport {
    pub termination: Termination,
    /// True only when the very first probe of the full input was SAT.
    pub input_satisfiable: bool,
    pub cores: Vec<FoundCore>,
    /// The still-UNSAT-but-not-yet-certified candidate preserved on early stop.
    #[serde(default)]
    pub retained_candidate: Vec<String>,
    /// Members never reached when budget/cancel stopped the walk.
    #[serde(default)]
    pub untested: Vec<String>,
    pub trace: Vec<TraceEntry>,
    pub solver: String,
    pub budget_limit: u64,
    pub budget_used: u64,
    pub rounds: usize,
    pub note: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExtractionOptions {
    #[serde(default)]
    pub mode: ExtractMode,
    /// Max solver decisions across the whole request; 0 = unlimited.
    #[serde(default)]
    pub budget: u64,
}

impl Default for ExtractionOptions {
    fn default() -> Self {
        Self { mode: ExtractMode::FindOne, budget: 0 }
    }
}

/// Fixed deterministic processing order: longest clauses first (a cheap
/// "constraints participating in more" heuristic), ties broken by input index.
fn ordered_indices(cnf: &Cnf) -> Vec<usize> {
    let mut idx: Vec<usize> = (0..cnf.constraints.len()).collect();
    idx.sort_by(|&a, &b| {
        cnf.constraints[b]
            .literals
            .len()
            .cmp(&cnf.constraints[a].literals.len())
            .then(a.cmp(&b))
    });
    idx
}

struct Accum {
    trace: Vec<TraceEntry>,
    seq: u64,
}

/// Everything describing one auditable decision except the verdict itself.
struct DecisionCtx<'a> {
    round: usize,
    phase: Phase,
    tested_id: Option<String>,
    trial_ids: &'a BTreeSet<String>,
    kept: Option<bool>,
}

impl Accum {
    fn log(
        &mut self,
        d: &DecisionCtx<'_>,
        r: &crate::solver::SolveResult,
        solver: &str,
        budget_used: u64,
    ) {
        self.seq += 1;
        self.trace.push(TraceEntry {
            seq: self.seq,
            round: d.round,
            phase: d.phase,
            tested_id: d.tested_id.clone(),
            trial_ids: d.trial_ids.iter().cloned().collect(),
            verdict: format!("{:?}", r.status).to_lowercase(),
            kept: d.kept,
            solver: solver.to_string(),
            budget_used_after: budget_used,
            detail: r.detail.clone(),
        });
    }
}

fn ids(cnf: &Cnf) -> BTreeSet<String> {
    cnf.constraints.iter().map(|c| c.id.clone()).collect()
}

/// Run extraction. Never panics on solver behaviour; every inconclusive path is
/// represented explicitly in the returned report.
#[must_use]
pub fn extract(
    cnf: &Cnf,
    solver: &dyn Solver,
    options: &ExtractionOptions,
    cancel: &CancelToken,
) -> ExtractionReport {
    let budget = Budget::new(options.budget);
    let ctx = SolveCtx::new(budget.clone(), cancel.clone());
    let solver_name = solver.name().to_string();
    let mut acc = Accum { trace: Vec::new(), seq: 0 };

    let order = ordered_indices(cnf);
    // `alive` = constraints not yet consumed by a packed core. The per-round walk
    // works on a *clone* of it; round deletions are only committed once a core is
    // certified and packed.
    let mut alive: BTreeSet<String> = ids(cnf);
    let mut cores: Vec<FoundCore> = Vec::new();
    let mut rounds = 0usize;

    // Reused across rounds; holds minimality witnesses for the current core.
    loop {
        rounds += 1;
        let round = rounds;

        // --- 1. Full-formula probe -------------------------------------------
        let probe = {
            let sub = cnf.subset(&alive);
            solver.solve(&sub, &ctx)
        };
        acc.log(
            &DecisionCtx {
                round,
                phase: Phase::FullCheck,
                tested_id: None,
                trial_ids: &alive,
                kept: None,
            },
            &probe,
            &solver_name,
            budget.used(),
        );

        match probe.status {
            SStatus::Sat => {
                if round == 1 {
                    return finish(
                        Termination::Satisfiable,
                        cores,
                        Vec::new(),
                        Vec::new(),
                        acc,
                        &solver_name,
                        &budget,
                        rounds,
                        Some("input formula is satisfiable; no unsat core exists".to_string()),
                        true,
                    );
                }
                // Remaining formula after pivot removal is SAT: diversity loop done.
                return finish(
                    Termination::Completed,
                    cores,
                    Vec::new(),
                    Vec::new(),
                    acc,
                    &solver_name,
                    &budget,
                    rounds,
                    Some("remaining formula after pivot elimination is satisfiable".to_string()),
                    false,
                );
            }
            SStatus::Unsat => { /* proceed to deletion */ }
            SStatus::Unknown => {
                let term = if cancel.is_cancelled() {
                    Termination::Cancelled
                } else if budget.exhausted() {
                    Termination::BudgetExhausted
                } else {
                    Termination::SolverUnknown
                };
                // On the very first probe `alive` has *never* been verified UNSAT,
                // so it must not be presented as a retained UNSAT candidate.
                let retained = if round == 1 { Vec::new() } else { alive.iter().cloned().collect() };
                return finish(
                    term,
                    cores,
                    retained,
                    Vec::new(),
                    acc,
                    &solver_name,
                    &budget,
                    rounds,
                    probe.detail.clone(),
                    false,
                );
            }
        }

        // --- 2. Deletion walk --------------------------------------------------
        // The walk mutates a *round-local* candidate set that starts from `alive`
        // (= formula minus all previously packed cores). Constraints deleted during
        // this round are not physically discarded from `alive` until the core is
        // certified and packed, so a member of an unrelated conflict cannot be
        // silently swallowed here.
        // witnesses collected for kept members against their trial set; these are
        // valid for `core \ {m}` because the final core is a subset of every later
        // surviving set.
        let mut witnesses: BTreeMap<String, Model> = BTreeMap::new();
        let mut untested: Vec<String> = Vec::new();
        let mut early: Option<Termination> = None;
        let mut candidate: BTreeSet<String> = alive.clone();

        for &i in &order {
            let cid = &cnf.constraints[i].id;
            if !alive.contains(cid) || !candidate.contains(cid) {
                continue; // already packed, or tentatively deleted this round
            }
            let mut early_term: Option<Termination> = None;
            if cancel.is_cancelled() {
                early_term = Some(Termination::Cancelled);
            } else if budget.exhausted() {
                early_term = Some(Termination::BudgetExhausted);
            }
            if let Some(term) = early_term {
                // This member and every still-present member later in the order were
                // never decided — record them all as untested.
                let mut reached = false;
                for &j in &order {
                    let id = &cnf.constraints[j].id;
                    if id == cid {
                        reached = true;
                    }
                    if reached && alive.contains(id) && candidate.contains(id) {
                        untested.push(id.clone());
                    }
                }
                early = Some(term);
                break;
            }

            let mut trial = candidate.clone();
            trial.remove(cid);

            let r = solver.solve(&cnf.subset(&trial), &ctx);
            let status = r.status;
            let decision = |kept: Option<bool>| DecisionCtx {
                round,
                phase: Phase::Deletion,
                tested_id: Some(cid.clone()),
                trial_ids: &trial,
                kept,
            };
            match status {
                SStatus::Unsat => {
                    candidate.remove(cid);
                    acc.log(&decision(Some(false)), &r, &solver_name, budget.used());
                }
                SStatus::Sat => {
                    acc.log(&decision(Some(true)), &r, &solver_name, budget.used());
                    if let Some(m) = r.model {
                        witnesses.insert(cid.clone(), m);
                    }
                }
                SStatus::Unknown => {
                    // Keep the member (cannot justify deleting it) and stop.
                    // The member itself was tested (verdict unknown); only members
                    // later in the order are genuinely untested.
                    let term = if cancel.is_cancelled() {
                        Termination::Cancelled
                    } else if budget.exhausted() {
                        Termination::BudgetExhausted
                    } else {
                        Termination::SolverUnknown
                    };
                    acc.log(&decision(Some(true)), &r, &solver_name, budget.used());
                    let mut passed = false;
                    for &j in &order {
                        let id = &cnf.constraints[j].id;
                        if id == cid {
                            passed = true;
                            continue;
                        }
                        if passed && alive.contains(id) && candidate.contains(id) {
                            untested.push(id.clone());
                        }
                    }
                    early = Some(term);
                    break;
                }
            }
        }

        let mut core_ids: Vec<String> = candidate.iter().cloned().collect();
        core_ids.sort_by_key(|id| pos(cnf, id));

        // --- 3. Minimality pass: fresh witness for C \ {m} for every member ----
        let mut certified = true;
        let mut stop_rounds = false;
        if let Some(term) = early {
            cores.push(FoundCore {
                member_ids: core_ids.clone(),
                size: core_ids.len(),
                verdict: CoreVerdict::UncertifiedUnsatCandidate,
                minimality_witnesses: witnesses,
            });
            return finish(
                term,
                cores,
                candidate.iter().cloned().collect(),
                untested,
                acc,
                &solver_name,
                &budget,
                rounds,
                Some("extraction interrupted; retained UNSAT candidate and proofs gathered so far"
                    .to_string()),
                false,
            );
        }

        for m in &core_ids {
            if cancel.is_cancelled() || budget.exhausted() {
                certified = false;
                stop_rounds = true;
                break;
            }
            let mut trial: BTreeSet<String> = core_ids.iter().cloned().collect();
            trial.remove(m);
            let r = solver.solve(&cnf.subset(&trial), &ctx);
            let status = r.status;
            let decision = |kept: Option<bool>| DecisionCtx {
                round,
                phase: Phase::MinimalityPass,
                tested_id: Some(m.clone()),
                trial_ids: &trial,
                kept,
            };
            match status {
                SStatus::Sat => {
                    acc.log(&decision(Some(true)), &r, &solver_name, budget.used());
                    if let Some(model) = r.model {
                        witnesses.insert(m.clone(), model);
                    }
                }
                SStatus::Unsat => {
                    // Theoretically impossible for an irreducible surviving set;
                    // record and leave the core uncertified rather than lying.
                    certified = false;
                    stop_rounds = true;
                    acc.log(&decision(Some(false)), &r, &solver_name, budget.used());
                }
                SStatus::Unknown => {
                    certified = false;
                    stop_rounds = true;
                    acc.log(&decision(None), &r, &solver_name, budget.used());
                }
            }
        }

        cores.push(FoundCore {
            size: core_ids.len(),
            verdict: if certified {
                CoreVerdict::CertifiedMus
            } else {
                CoreVerdict::UncertifiedUnsatCandidate
            },
            minimality_witnesses: witnesses,
            member_ids: core_ids,
        });

        if !certified {
            let term = if cancel.is_cancelled() {
                Termination::Cancelled
            } else if budget.exhausted() {
                Termination::BudgetExhausted
            } else {
                Termination::SolverUnknown
            };
            return finish(
                term,
                cores,
                Vec::new(),
                Vec::new(),
                acc,
                &solver_name,
                &budget,
                rounds,
                None,
                false,
            );
        }

        match options.mode {
            ExtractMode::FindOne => {
                return finish(
                    Termination::Completed,
                    cores,
                    Vec::new(),
                    Vec::new(),
                    acc,
                    &solver_name,
                    &budget,
                    rounds,
                    None,
                    false,
                );
            }
            ExtractMode::FindAll => {
                if stop_rounds {
                    let term = if cancel.is_cancelled() {
                        Termination::Cancelled
                    } else {
                        Termination::BudgetExhausted
                    };
                    return finish(
                        term,
                        cores,
                        Vec::new(),
                        Vec::new(),
                        acc,
                        &solver_name,
                        &budget,
                        rounds,
                        None,
                        false,
                    );
                }
                // Core packing: drop the ENTIRE found core (not just a pivot).
                // Removing only one pivot lets the next deletion walk discard the
                // pivot's partners once another conflict keeps the formula UNSAT,
                // which silently collapses to a single core. Removing the whole
                // certified core guarantees every later core is *disjoint* from
                // every earlier one (a maximal, order-dependent core packing — a
                // diversity strategy, not MUS enumeration: overlapping MUSes whose
                // members are consumed by an earlier core are not rediscovered).
                let found: BTreeSet<String> = cores
                    .last()
                    .expect("core just pushed")
                    .member_ids
                    .iter()
                    .cloned()
                    .collect();
                for m in &found {
                    alive.remove(m);
                }
                if alive.is_empty() {
                    return finish(
                        Termination::Completed,
                        cores,
                        Vec::new(),
                        Vec::new(),
                        acc,
                        &solver_name,
                        &budget,
                        rounds,
                        Some("no constraints left after core packing".to_string()),
                        false,
                    );
                }
            }
        }
    }
}

fn pos(cnf: &Cnf, id: &str) -> usize {
    cnf.constraints.iter().position(|c| c.id == id).unwrap_or(usize::MAX)
}

#[allow(clippy::too_many_arguments)]
fn finish(
    termination: Termination,
    cores: Vec<FoundCore>,
    retained_candidate: Vec<String>,
    untested: Vec<String>,
    acc: Accum,
    solver_name: &str,
    budget: &Budget,
    rounds: usize,
    note: Option<String>,
    input_satisfiable: bool,
) -> ExtractionReport {
    ExtractionReport {
        termination,
        input_satisfiable,
        cores,
        retained_candidate,
        untested,
        trace: acc.trace,
        solver: solver_name.to_string(),
        budget_limit: budget.limit(),
        budget_used: budget.used(),
        rounds,
        note,
    }
}
