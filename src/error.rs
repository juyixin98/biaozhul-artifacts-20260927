//! Typed error contract shared by the kernel and the HTTP layer.
//!
//! Every error has a stable string `code` (asserted in tests), a [`Category`]
//! and a suggested HTTP status. Keeping the four categories explicit lets
//! callers distinguish bad input from conflicting state, exhaustion and
//! internal computation failures.

use serde::Serialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Category {
    Input,
    State,
    Resource,
    Computation,
    NotFound,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KernelError {
    pub code: &'static str,
    pub category: Category,
    pub message: String,
}

impl KernelError {
    pub(crate) fn input(code: &'static str, msg: impl Into<String>) -> Self {
        KernelError {
            code,
            category: Category::Input,
            message: msg.into(),
        }
    }
    pub(crate) fn state(code: &'static str, msg: impl Into<String>) -> Self {
        KernelError {
            code,
            category: Category::State,
            message: msg.into(),
        }
    }
    pub(crate) fn resource(code: &'static str, msg: impl Into<String>) -> Self {
        KernelError {
            code,
            category: Category::Resource,
            message: msg.into(),
        }
    }
    pub(crate) fn computation(msg: impl Into<String>) -> Self {
        KernelError {
            code: "COMPUTATION_FAILED",
            category: Category::Computation,
            message: msg.into(),
        }
    }

    pub fn http_status(&self) -> u16 {
        match self.category {
            Category::Input => 400,
            Category::State => 409,
            Category::Resource => 507,
            Category::Computation => 500,
            Category::NotFound => 404,
        }
    }

    pub fn category_name(&self) -> &'static str {
        match self.category {
            Category::Input => "input",
            Category::State => "state",
            Category::Resource => "resource",
            Category::Computation => "computation",
            Category::NotFound => "not_found",
        }
    }

    // ---- input errors ---------------------------------------------------
    pub fn empty_ruleset() -> Self {
        Self::input("EMPTY_RULESET", "ruleset must declare at least one rule")
    }
    pub fn bad_rule_id(id: &str) -> Self {
        Self::input(
            "BAD_RULE_ID",
            format!("invalid rule id {id:?}: 1..=64 chars, [A-Za-z0-9_-]"),
        )
    }
    pub fn duplicate_rule_id(id: &str) -> Self {
        Self::input(
            "DUPLICATE_RULE_ID",
            format!("rule id {id:?} appears more than once"),
        )
    }
    pub fn bad_window(id: &str) -> Self {
        Self::input(
            "BAD_WINDOW",
            format!("rule {id:?}: after>=0 and within/duration>=1 required"),
        )
    }
    pub fn unknown_rule(id: &str) -> Self {
        Self::input(
            "UNKNOWN_RULE",
            format!("event references unknown rule id {id:?}"),
        )
    }
    pub fn empty_event() -> Self {
        Self::input("EMPTY_EVENT", "event must carry at least one attribute")
    }
    pub fn step_gap(expected: i64, got: i64) -> Self {
        Self::input(
            "STEP_GAP",
            format!("non-contiguous step: expected {expected}, got {got}"),
        )
    }

    // ---- state conflicts ------------------------------------------------
    pub fn step_regressed(expected: i64, got: i64) -> Self {
        Self::state(
            "STEP_REGRESSED",
            format!("event step {got} already passed; next expected is {expected}"),
        )
    }
    pub fn monitor_closed() -> Self {
        Self::state(
            "MONITOR_CLOSED",
            "trace already ended; no more events accepted",
        )
    }
    pub fn version_mismatch(expected: &str, got: &str) -> Self {
        Self::state(
            "VERSION_MISMATCH",
            format!("ruleset version mismatch: state belongs to {expected:?}, request has {got:?}"),
        )
    }
    pub fn snapshot_digest() -> Self {
        Self::state(
            "SNAPSHOT_DIGEST_MISMATCH",
            "snapshot digest does not match its content",
        )
    }
    pub fn decision_chain_broken() -> Self {
        Self::state(
            "DECISION_CHAIN_BROKEN",
            "decision log hash chain does not verify",
        )
    }
    pub fn snapshot_id_mismatch(a: &str, b: &str) -> Self {
        Self::state(
            "SNAPSHOT_ID_MISMATCH",
            format!("cannot restore snapshot of monitor {a:?} into monitor {b:?}"),
        )
    }

    // ---- resource exhaustion -------------------------------------------
    pub fn obligation_limit(n: usize) -> Self {
        Self::resource(
            "OBLIGATION_LIMIT",
            format!("active obligation limit ({n}) reached; close or rotate to release"),
        )
    }
    pub fn epoch_limit(n: usize) -> Self {
        Self::resource(
            "EPOCH_LIMIT",
            format!("epoch/history limit ({n}) reached; snapshot and start a new monitor"),
        )
    }
    pub fn log_limit(bytes: usize) -> Self {
        Self::resource(
            "DECISION_LOG_LIMIT",
            format!("decision log budget ({bytes} bytes) exhausted"),
        )
    }

    // ---- not found ------------------------------------------------------
    pub fn not_found(what: &str, id: &str) -> Self {
        KernelError {
            code: "NOT_FOUND",
            category: Category::NotFound,
            message: format!("{what} {id:?} not found"),
        }
    }
}

impl std::fmt::Display for KernelError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "[{}] {}", self.code, self.message)
    }
}
impl std::error::Error for KernelError {}

pub type Result<T> = std::result::Result<T, KernelError>;

/// Stable JSON error body returned by every HTTP endpoint.
#[derive(Debug, Serialize)]
pub struct ErrorBody {
    pub error: ErrorDetail,
}

#[derive(Debug, Serialize)]
pub struct ErrorDetail {
    pub code: String,
    pub category: String,
    pub message: String,
    pub run_id: String,
}
