//! Test-only shared infrastructure.
#![allow(dead_code)]
//!
//! IMPORTANT: the independent oracle here is written from scratch and shares NO
//! solving code with the crate's extraction kernel (`mus_core::solver`). It
//! evaluates clauses directly and enumerates subsets/power-sets on its own, so the
//! reference answers in the test-suite are genuinely produced by a different
//! implementation — the system under test cannot generate its own answer key.

use mus_core::language::{Cnf, Literal, Model};
use mus_core::solver::{SStatus, SolveCtx, SolveResult, Solver};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

// ---------------------------------------------------------------------------
// Independent evaluator (not using Cnf::satisfied_by)
// ---------------------------------------------------------------------------

/// Evaluate one literal under a total assignment given as bits (bit v-1 = value v).
fn eval_lit_bits(signed: i32, bits: u64) -> bool {
    let v = signed.unsigned_abs() as usize;
    let bit = (bits >> (v - 1)) & 1 == 1;
    if signed > 0 { bit } else { !bit }
}

/// Independent SAT oracle: brute-force truth tables, own clause evaluation.
/// Refuses instances over 24 variables with Unknown.
pub struct Enumerator;

pub const ENUM_MAX_NVARS: usize = 24;

impl Enumerator {
    /// Returns Some(satisfying bits) / None(UNSAT) / None-ish unknown via Result.
    pub fn decide(cnf: &Cnf) -> Result<Option<u64>, &'static str> {
        if cnf.nvars > ENUM_MAX_NVARS {
            return Err("enumerator refuses nvars > 24");
        }
        if cnf.constraints.iter().any(|c| c.literals.is_empty()) {
            return Ok(None);
        }
        let total: u64 = if cnf.nvars == 0 {
            1
        } else {
            1u64.checked_shl(cnf.nvars as u32).ok_or("shift overflow")?
        };
        for bits in 0..total {
            let ok = cnf.constraints.iter().all(|c| c.literals.iter().any(|l| eval_lit_bits(l.0, bits)));
            if ok {
                return Ok(Some(bits));
            }
        }
        Ok(None)
    }

    pub fn is_sat(cnf: &Cnf) -> bool {
        matches!(Self::decide(cnf), Ok(Some(_)))
    }

    pub fn is_unsat(cnf: &Cnf) -> bool {
        matches!(Self::decide(cnf), Ok(None))
    }

    /// Convert satisfying bits to a [`Model`].
    pub fn model_of(cnf: &Cnf, bits: u64) -> Model {
        let mut m = vec![true; cnf.nvars + 1];
        for (v, slot) in m.iter_mut().enumerate().skip(1) {
            *slot = (bits >> (v - 1)) & 1 == 1;
        }
        Model(m)
    }

    /// Enumerate **every** subset-minimal UNSAT core of `cnf` (as id-sets),
    /// independently: iterate subsets in increasing size and keep UNSAT sets that
    /// contain no smaller UNSAT set. Exponential — fixtures only.
    pub fn all_mus(cnf: &Cnf) -> Vec<BTreeSet<String>> {
        let ids: Vec<&str> = cnf.constraints.iter().map(|c| c.id.as_str()).collect();
        let n = ids.len();
        let mut found: Vec<BTreeSet<String>> = Vec::new();

        'outer: for mask in 1u32..(1u32 << n) {
            let set: BTreeSet<String> = (0..n)
                .filter(|i| (mask >> i) & 1 == 1)
                .map(|i| ids[i].to_string())
                .collect();
            let sub = cnf.subset(&set);
            if !Self::is_unsat(&sub) {
                continue;
            }
            // Minimal iff it contains no previously found MUS.
            for m in &found {
                if m.is_subset(&set) {
                    continue 'outer;
                }
            }
            found.push(set);
        }
        found
    }

    /// Is `set` subset-minimal UNSAT per the independent enumerator?
    pub fn is_mus(cnf: &Cnf, set: &BTreeSet<String>) -> bool {
        let sub = cnf.subset(set);
        if !Self::is_unsat(&sub) {
            return false;
        }
        for m in set {
            let mut trial = set.clone();
            trial.remove(m);
            if Self::is_unsat(&cnf.subset(&trial)) {
                return false;
            }
        }
        true
    }
}

// ---------------------------------------------------------------------------
// Scripted solver kernels for adversarial tests
// ---------------------------------------------------------------------------

/// Solver whose answers come from a script keyed by the set of constraint ids.
/// Unscripted queries fall back to `default`; calls are counted.
pub struct ScriptedSolver {
    pub name: String,
    script: Mutex<BTreeMap<BTreeSet<String>, SStatus>>,
    pub calls: AtomicU64,
    pub default: SStatus,
}

impl ScriptedSolver {
    pub fn new(default: SStatus) -> Self {
        Self {
            name: "scripted".to_string(),
            script: Mutex::new(BTreeMap::new()),
            calls: AtomicU64::new(0),
            default,
        }
    }

    pub fn named(mut self, name: &str) -> Self {
        self.name = name.to_string();
        self
    }

    /// Script an answer for the trial formula containing exactly these ids.
    pub fn on<const N: usize>(self, ids: [&str; N], status: SStatus) -> Self {
        let key: BTreeSet<String> = ids.iter().map(|s| s.to_string()).collect();
        self.script.lock().unwrap().insert(key, status);
        self
    }

    /// Script an answer for a dynamically built id-set.
    pub fn on_const(self, ids: &BTreeSet<String>, status: SStatus) -> Self {
        self.script.lock().unwrap().insert(ids.clone(), status);
        self
    }
}

impl Solver for ScriptedSolver {
    fn name(&self) -> &str {
        &self.name
    }

    fn solve(&self, cnf: &Cnf, ctx: &SolveCtx) -> SolveResult {
        if !ctx.budget.tick() {
            return SolveResult::unknown("budget exhausted (scripted)");
        }
        if ctx.cancel.is_cancelled() {
            return SolveResult::unknown("cancelled (scripted)");
        }
        self.calls.fetch_add(1, Ordering::SeqCst);
        let key: BTreeSet<String> = cnf.constraints.iter().map(|c| c.id.clone()).collect();
        let status = *self.script.lock().unwrap().get(&key).unwrap_or(&self.default);
        match status {
            SStatus::Sat => {
                // Produce a real witness via the independent enumerator; if that says
                // UNSAT, the script is contradictory — surface Unknown honestly.
                match Enumerator::decide(cnf) {
                    Ok(Some(bits)) => SolveResult::sat(Enumerator::model_of(cnf, bits)),
                    _ => SolveResult::unknown("script said SAT but independent witness unavailable"),
                }
            }
            SStatus::Unsat => SolveResult::unsat(),
            SStatus::Unknown => SolveResult::unknown("scripted unknown"),
        }
    }
}

/// Solver that reports a fixed status on every query regardless of truth — used to
/// prove the pipeline never trusts an unproven UNSAT at the verification boundary.
pub struct LyingSolver {
    pub always: SStatus,
    pub name: String,
}

impl LyingSolver {
    pub fn new(always: SStatus) -> Self {
        Self { always, name: "lying-solver".to_string() }
    }
}

impl Solver for LyingSolver {
    fn name(&self) -> &str {
        &self.name
    }
    fn solve(&self, _cnf: &Cnf, ctx: &SolveCtx) -> SolveResult {
        if !ctx.budget.tick() {
            return SolveResult::unknown("budget");
        }
        match self.always {
            SStatus::Sat => SolveResult::sat(Model(vec![true; _cnf.nvars + 1])),
            SStatus::Unsat => SolveResult::unsat(),
            SStatus::Unknown => SolveResult::unknown("always-unknown"),
        }
    }
}

/// Solver that blocks until its cancellation token is set (or a timeout), proving
/// cancellation is actually cooperatively observed.
pub struct StallSolver {
    pub name: String,
    pub observed_cancel: Arc<AtomicBool>,
}

impl StallSolver {
    pub fn new() -> (Self, Arc<AtomicBool>) {
        let flag = Arc::new(AtomicBool::new(false));
        (
            Self { name: "stall".to_string(), observed_cancel: flag.clone() },
            flag,
        )
    }
}

impl Solver for StallSolver {
    fn name(&self) -> &str {
        &self.name
    }
    fn solve(&self, cnf: &Cnf, ctx: &SolveCtx) -> SolveResult {
        if !ctx.budget.tick() {
            return SolveResult::unknown("budget");
        }
        // Busy-wait for cancel, bounded so a test failure cannot hang forever.
        let mut spins = 0u64;
        while !ctx.cancel.is_cancelled() {
            std::thread::sleep(std::time::Duration::from_millis(2));
            spins += 1;
            if spins > 5_000 {
                return SolveResult::unknown("stall timeout without cancel");
            }
        }
        self.observed_cancel.store(true, Ordering::SeqCst);
        let _ = cnf;
        SolveResult::unknown("cancelled mid-search")
    }
}

// ---------------------------------------------------------------------------
// Enumerator exposed through the Solver trait (the test's independent oracle)
// ---------------------------------------------------------------------------

pub struct EnumSolver;

impl Solver for EnumSolver {
    fn name(&self) -> &str {
        "test-independent-enumerator"
    }

    fn solve(&self, cnf: &Cnf, ctx: &SolveCtx) -> SolveResult {
        if !ctx.budget.tick() {
            return SolveResult::unknown("enumerator budget exhausted");
        }
        if ctx.cancel.is_cancelled() {
            return SolveResult::unknown("cancelled");
        }
        match Enumerator::decide(cnf) {
            Ok(Some(bits)) => SolveResult::sat(Enumerator::model_of(cnf, bits)),
            Ok(None) => SolveResult::unsat(),
            Err(e) => SolveResult::unknown(e),
        }
    }
}

/// First decision UNSAT (and budgeted), then blocks until cancelled. Models the
/// case where a verified UNSAT candidate exists when cancel arrives.
pub struct UnsatThenStall {
    pub name: String,
    pub calls: AtomicU64,
    pub observed_cancel: Arc<AtomicBool>,
}

impl UnsatThenStall {
    pub fn new() -> (Self, Arc<AtomicBool>) {
        let flag = Arc::new(AtomicBool::new(false));
        (
            Self {
                name: "unsat-then-stall".to_string(),
                calls: AtomicU64::new(0),
                observed_cancel: flag.clone(),
            },
            flag,
        )
    }
}

impl Solver for UnsatThenStall {
    fn name(&self) -> &str {
        &self.name
    }
    fn solve(&self, _cnf: &Cnf, ctx: &SolveCtx) -> SolveResult {
        if !ctx.budget.tick() {
            return SolveResult::unknown("budget");
        }
        let n = self.calls.fetch_add(1, Ordering::SeqCst);
        if n == 0 {
            return SolveResult::unsat();
        }
        let mut spins = 0u64;
        while !ctx.cancel.is_cancelled() {
            std::thread::sleep(std::time::Duration::from_millis(2));
            spins += 1;
            if spins > 5_000 {
                return SolveResult::unknown("stall timeout without cancel");
            }
        }
        self.observed_cancel.store(true, Ordering::SeqCst);
        SolveResult::unknown("cancelled mid-search")
    }
}

/// Build a [`BTreeMap<String, Model>`]-style witness check independently:
/// verify a witness bitset satisfies exactly the given subset.
pub fn witness_satisfies(cnf: &Cnf, set: &BTreeSet<String>, bits: u64) -> bool {
    let sub = cnf.subset(set);
    sub.constraints
        .iter()
        .all(|c| c.literals.iter().any(|Literal(s)| eval_lit_bits(*s, bits)))
}
