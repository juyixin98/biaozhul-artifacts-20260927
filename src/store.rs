//! In-memory monitor registry.
//!
//! The store is deliberately local (no external services): each entry keeps
//! the monitor, the rule set and the full submitted trace so evidence bundles
//! can be produced and replayed.  State conflicts (unknown / closed monitors)
//! and resource exhaustion (too many monitors) are raised explicitly.

use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard};

use crate::error::{AppError, AppResult};
use crate::kernel::{Limits, Monitor, StepOutcome, Verdict};
use crate::language::{Ruleset, Step};

pub struct MonitorEntry {
    pub monitor: Monitor,
    pub ruleset: Ruleset,
    pub trace: Vec<Step>,
}

/// Shared application state.
#[derive(Default)]
pub struct Store {
    inner: Mutex<HashMap<String, MonitorEntry>>,
}

impl Store {
    pub fn new() -> Self {
        Self::default()
    }

    fn lock(&self) -> AppResult<MutexGuard<'_, HashMap<String, MonitorEntry>>> {
        self.inner.lock().map_err(|_| {
            AppError::compute("store_lock_poisoned", "monitor registry mutex was poisoned")
        })
    }

    /// Create a monitor under `id`.
    pub fn create(&self, id: String, ruleset: Ruleset, limits: Limits) -> AppResult<Monitor> {
        let mut map = self.lock()?;
        if map.contains_key(&id) {
            return Err(AppError::conflict(
                "monitor_id_exists",
                format!("monitor `{id}` already exists"),
            ));
        }
        if map.len() >= limits.max_monitors {
            return Err(AppError::exhausted(
                "max_monitors_exceeded",
                format!("monitor cap {} reached", limits.max_monitors),
            ));
        }
        let monitor = Monitor::new(ruleset.clone(), limits)?;
        map.insert(id, MonitorEntry { monitor: monitor.clone(), ruleset, trace: Vec::new() });
        Ok(monitor)
    }

    /// Register an already-restored monitor (snapshot recovery).  The trace
    /// is re-derived empty: further steps append normally, and evidence cuts
    /// replay from the restored snapshot state via `/evaluate` + `/verify`.
    pub fn insert_restored(
        &self,
        id: String,
        ruleset: Ruleset,
        monitor: Monitor,
    ) -> AppResult<()> {
        let mut map = self.lock()?;
        if map.contains_key(&id) {
            return Err(AppError::conflict(
                "monitor_id_exists",
                format!("monitor `{id}` already exists"),
            ));
        }
        if map.len() >= monitor.limits().max_monitors {
            return Err(AppError::exhausted(
                "max_monitors_exceeded",
                format!("monitor cap {} reached", monitor.limits().max_monitors),
            ));
        }
        map.insert(id, MonitorEntry { monitor, ruleset, trace: Vec::new() });
        Ok(())
    }

    fn with_entry<T>(
        &self,
        id: &str,
        f: impl FnOnce(&mut MonitorEntry) -> AppResult<T>,
    ) -> AppResult<T> {
        let mut map = self.lock()?;
        let entry = map
            .get_mut(id)
            .ok_or_else(|| AppError::conflict("unknown_monitor", format!("no monitor `{id}`")))?;
        f(entry)
    }

    /// Apply a step and record it in the trace.
    pub fn apply_step(&self, id: &str, step: &Step) -> AppResult<StepOutcome> {
        self.with_entry(id, |entry| {
            let outcome = entry.monitor.apply_step(step)?;
            entry.trace.push(step.clone());
            Ok(outcome)
        })
    }

    /// Close the trace by submitting an explicit bare end marker as the next
    /// step.
    pub fn close(&self, id: &str) -> AppResult<StepOutcome> {
        self.with_entry(id, |entry| {
            let marker = Step {
                index: entry.monitor.step_count(),
                event: crate::language::Event::default(),
                ruleset_version: None,
                end: true,
            };
            let outcome = entry.monitor.apply_step(&marker)?;
            entry.trace.push(marker);
            Ok(outcome)
        })
    }

    pub fn snapshot(&self, id: &str) -> AppResult<crate::kernel::SavedMonitor> {
        self.with_entry(id, |entry| Ok(entry.monitor.snapshot()))
    }

    pub fn get(&self, id: &str) -> AppResult<MonitorEntrySnapshot> {
        self.with_entry(id, |entry| {
            Ok(MonitorEntrySnapshot {
                monitor_id: id.to_string(),
                ruleset: entry.ruleset.clone(),
                ruleset_hash: entry.monitor.ruleset_hash.clone(),
                verdict: entry.monitor.verdict(),
                closed: entry.monitor.is_closed(),
                step_count: entry.monitor.step_count(),
                obligations: entry.monitor.obligations(),
            })
        })
    }

    pub fn evidence(
        &self,
        id: &str,
        run_id: &str,
        snapshot_after_index: Option<u64>,
    ) -> AppResult<crate::evidence::EvidenceBundle> {
        self.with_entry(id, |entry| {
            let cut = match snapshot_after_index {
                None => None,
                Some(c) => {
                    if c as usize > entry.trace.len() {
                        return Err(AppError::input(
                            "snapshot_index_out_of_range",
                            format!("cut {c} beyond {} applied steps", entry.trace.len()),
                        ));
                    }
                    // Recompute snapshot by driving a fresh monitor to the
                    // cut — evidence must be independently reproducible.
                    let mut cut_monitor =
                        Monitor::new(entry.ruleset.clone(), entry.monitor.limits().clone())?;
                    for step in &entry.trace[..c as usize] {
                        cut_monitor.apply_step(step)?;
                    }
                    Some((c, cut_monitor))
                }
            };
            let (snapshot, online_at_cut) = match cut {
                None => (None, entry.monitor.obligations()),
                Some((_c, m)) => (Some(m.snapshot()), m.obligations()),
            };
            Ok(crate::evidence::EvidenceBundle {
                run_id: run_id.to_string(),
                ruleset: entry.ruleset.clone(),
                trace: entry.trace.clone(),
                snapshot_after_index,
                snapshot,
                online_obligations: online_at_cut,
                claimed_verdict: entry.monitor.verdict(),
            })
        })
    }

    pub fn len(&self) -> AppResult<usize> {
        Ok(self.lock()?.len())
    }

    pub fn is_empty(&self) -> AppResult<bool> {
        Ok(self.lock()?.is_empty())
    }
}

/// Observer-facing monitor summary.
#[derive(Debug, Clone, serde::Serialize)]
pub struct MonitorEntrySnapshot {
    pub monitor_id: String,
    pub ruleset: Ruleset,
    pub ruleset_hash: String,
    pub verdict: Verdict,
    pub closed: bool,
    pub step_count: u64,
    pub obligations: Vec<crate::kernel::ObligationView>,
}
