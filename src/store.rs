//! In-memory constraint store with atomic batch mutations.
//!
//! Concurrency is a single [`std::sync::Mutex`] guarding a [`StoreInner`].
//! All handlers hold the lock for the whole batch, which gives the required
//! guarantee directly:
//!
//! * a batch validates **and applies** as one unit — if any item fails the
//!   store is left byte-for-byte unchanged, so no concurrent (or later in the
//!   same batch) request can ever observe a half-applied set;
//! * reads (list / solve / verify) take a consistent snapshot under the same
//!   lock, so a batch cannot be observed mid-flight.
//!
//! Resource limits are enforced against the post-mutation size *before*
//! anything is applied.

use std::collections::BTreeMap;

use crate::error::{ServiceError, ServiceResult};
use crate::model::Constraint;
use crate::solver::{MAX_EDGES, MAX_VERTICES};

#[derive(Debug, Clone, Default)]
pub struct StoreInner {
    /// Insertion order preserved via a parallel Vec; ids unique.
    constraints: BTreeMap<String, Constraint>,
    order: Vec<String>,
    revision: u64,
}

impl StoreInner {
    pub fn len(&self) -> usize {
        self.order.len()
    }

    pub fn is_empty(&self) -> bool {
        self.order.is_empty()
    }

    pub fn revision(&self) -> u64 {
        self.revision
    }

    pub fn list(&self) -> Vec<Constraint> {
        self.order
            .iter()
            .filter_map(|id| self.constraints.get(id))
            .cloned()
            .collect()
    }

    /// Snapshot by exact ids; `StateConflict` if any id is unknown.
    pub fn get_many(&self, ids: &[String]) -> ServiceResult<Vec<Constraint>> {
        let mut out = Vec::with_capacity(ids.len());
        for id in ids {
            match self.constraints.get(id) {
                Some(c) => out.push(c.clone()),
                None => {
                    return Err(ServiceError::state_conflict(format!(
                        "no constraint with id '{id}'"
                    )))
                }
            }
        }
        Ok(out)
    }

    /// All-constraints snapshot.
    pub fn snapshot(&self) -> Vec<Constraint> {
        self.list()
    }
}

#[derive(Debug)]
pub struct Store {
    inner: std::sync::Mutex<StoreInner>,
}

impl Clone for Store {
    /// Clones the current state snapshot (used when services are forked in
    /// tests); the clone is deliberately not shared with the original.
    fn clone(&self) -> Self {
        let inner = self.inner.lock().expect("store mutex poisoned").clone();
        Self {
            inner: std::sync::Mutex::new(inner),
        }
    }
}

impl Default for Store {
    fn default() -> Self {
        Self {
            inner: std::sync::Mutex::new(StoreInner::default()),
        }
    }
}

/// A single batch operation.
#[derive(Debug, Clone)]
pub enum BatchOp {
    /// Add a new constraint; fails with StateConflict if the id exists.
    Add(Constraint),
    /// Replace an existing constraint; fails with StateConflict if unknown.
    Update(Constraint),
    /// Remove a constraint; fails with StateConflict if unknown.
    Delete(String),
    /// Remove every constraint. Never fails.
    Clear,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum OpResult {
    Added { id: String },
    Updated { id: String },
    Deleted { id: String },
    Cleared { removed: usize },
}

impl Store {
    pub fn new() -> Self {
        Self::default()
    }

    /// Consistent read: run `f` against the current inner state.
    pub fn read<R>(&self, f: impl FnOnce(&StoreInner) -> R) -> R {
        let guard = self.inner.lock().expect("store mutex poisoned");
        f(&guard)
    }

    /// Apply one operation, atomically.
    pub fn apply_one(&self, op: BatchOp) -> ServiceResult<OpResult> {
        self.apply_batch(std::iter::once(op))
            .map(|mut v| v.pop().expect("one op in, one result out"))
    }

    /// Apply a batch atomically. Every operation is simulated first; on the
    /// first failure nothing is applied and the error is returned. On success
    /// results are returned in input order.
    pub fn apply_batch<I>(&self, ops: I) -> ServiceResult<Vec<OpResult>>
    where
        I: IntoIterator<Item = BatchOp>,
    {
        let ops: Vec<BatchOp> = ops.into_iter().collect();
        let mut guard = self.inner.lock().expect("store mutex poisoned");

        // ---- Simulation phase on a private working copy of the index ----
        // We clone the whole map (constraints are small); the cost is bounded
        // by MAX_EDGES and keeps rollback trivial: the real store is only
        // touched after all operations and limits are known to succeed.
        let mut working: BTreeMap<String, Constraint> = guard.constraints.clone();
        let mut working_order: Vec<String> = guard.order.clone();
        let mut results: Vec<OpResult> = Vec::with_capacity(ops.len());

        for op in &ops {
            match op {
                BatchOp::Add(c) => {
                    if working.contains_key(&c.id) {
                        return Err(ServiceError::state_conflict(format!(
                            "constraint id '{}' already exists",
                            c.id
                        ))
                        .with_detail(serde_json::json!({"conflicting_id": c.id})));
                    }
                    working_order.push(c.id.clone());
                    working.insert(c.id.clone(), c.clone());
                    results.push(OpResult::Added { id: c.id.clone() });
                }
                BatchOp::Update(c) => {
                    if !working.contains_key(&c.id) {
                        return Err(ServiceError::state_conflict(format!(
                            "no constraint with id '{}' to update",
                            c.id
                        )));
                    }
                    working.insert(c.id.clone(), c.clone());
                    results.push(OpResult::Updated { id: c.id.clone() });
                }
                BatchOp::Delete(id) => {
                    if working.remove(id).is_none() {
                        return Err(ServiceError::state_conflict(format!(
                            "no constraint with id '{id}' to delete"
                        )));
                    }
                    working_order.retain(|x| x != id);
                    results.push(OpResult::Deleted { id: id.clone() });
                }
                BatchOp::Clear => {
                    let removed = working.len();
                    working.clear();
                    working_order.clear();
                    results.push(OpResult::Cleared { removed });
                }
            }
        }

        // ---- Limit phase against the post-mutation state ----
        if working.len() > MAX_EDGES {
            return Err(ServiceError::resource_exhausted(format!(
                "batch would leave {} constraints, limit is {MAX_EDGES}",
                working.len()
            )));
        }
        // Count distinct variables in exactly the post-commit state.
        let mut vars = std::collections::BTreeSet::new();
        for c in working.values() {
            vars.insert(c.lhs.as_str());
            vars.insert(c.rhs.as_str());
        }
        if vars.len() > MAX_VERTICES {
            return Err(ServiceError::resource_exhausted(format!(
                "batch would leave {} variables, limit is {MAX_VERTICES}",
                vars.len()
            )));
        }

        // ---- Commit phase: cannot fail ----
        guard.constraints = working;
        guard.order = working_order;
        guard.revision += 1;
        Ok(results)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn c(id: &str, w: i64) -> Constraint {
        Constraint::new(id, "x", "y", w).unwrap()
    }

    #[test]
    fn failed_batch_leaves_store_untouched() {
        let store = Store::new();
        let r = store.apply_batch(vec![
            BatchOp::Add(c("a", 1)),
            BatchOp::Add(c("b", 2)),
            BatchOp::Delete("missing".to_string()),
        ]);
        assert!(r.is_err());
        assert_eq!(r.unwrap_err().kind, crate::error::ErrorKind::StateConflict);
        assert!(store.read(|s| s.is_empty()));
    }

    #[test]
    fn update_and_clear_are_atomic() {
        let store = Store::new();
        store.apply_one(BatchOp::Add(c("a", 1))).unwrap();
        let r = store.apply_batch(vec![
            BatchOp::Clear,
            BatchOp::Add(c("b", 2)),
            BatchOp::Update(c("a", 9)), // unknown after clear -> whole batch rolls back
        ]);
        assert_eq!(r.unwrap_err().kind, crate::error::ErrorKind::StateConflict);
        let list = store.read(|s| s.list());
        assert_eq!(list.len(), 1);
        assert_eq!(list[0].id, "a");
        assert_eq!(list[0].bound, 1);
    }
}
