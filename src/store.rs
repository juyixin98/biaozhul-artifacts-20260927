//! In-memory monitor repository.
//!
//! A single `std::sync::Mutex<HashMap>` is sufficient for a local service;
//! every public method takes the lock for its whole critical section, so the
//! monitor's internal transactionality (spawn→journal→commit) is atomic at
//! the HTTP boundary as well.

use std::collections::HashMap;
use std::sync::Mutex;

use crate::error::{KernelError, Result};
use crate::monitor::Monitor;

#[derive(Default)]
pub struct Store {
    monitors: Mutex<HashMap<String, Monitor>>,
}

impl Store {
    pub fn new() -> Self {
        Store {
            monitors: Mutex::new(HashMap::new()),
        }
    }

    pub fn insert(&self, m: Monitor) -> Result<()> {
        let id = m.id.clone();
        let mut guard = self.monitors.lock().expect("store mutex poisoned");
        if guard.contains_key(&id) {
            return Err(KernelError::state(
                "MONITOR_EXISTS",
                format!("monitor {id:?} already exists"),
            ));
        }
        guard.insert(id, m);
        Ok(())
    }

    pub fn take(&self, id: &str) -> Result<Monitor> {
        let mut guard = self.monitors.lock().expect("store mutex poisoned");
        guard
            .remove(id)
            .ok_or_else(|| KernelError::not_found("monitor", id))
    }

    /// Run a closure against a borrowed monitor. The error/result contract is
    /// the kernel's own; the lock is released on return.
    pub fn with<T>(&self, id: &str, f: impl FnOnce(&mut Monitor) -> Result<T>) -> Result<T> {
        let mut guard = self.monitors.lock().expect("store mutex poisoned");
        let m = guard
            .get_mut(id)
            .ok_or_else(|| KernelError::not_found("monitor", id))?;
        f(m)
    }

    pub fn list(&self) -> Vec<String> {
        let guard = self.monitors.lock().expect("store mutex poisoned");
        guard.keys().cloned().collect()
    }
}
