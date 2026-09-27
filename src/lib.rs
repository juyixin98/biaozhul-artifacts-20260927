//! Bounded temporal rule monitor over finite traces.
//!
//! Crate layout:
//! - [`error`]: the four-class error contract shared by every module.
//! - [`language`]: input language DTOs (rule sets, rules, predicates, steps).
//! - [`matcher`]: predicate/event-pattern evaluation primitive.
//! - [`kernel`]: online monitoring kernel (obligation instances, three
//!   valued verdicts, snapshots/recovery).
//! - [`oracle`]: independent offline unfold implementation used as the
//!   reference answer.
//! - [`evidence`]: replayable evidence bundles and their verification.
//! - [`store`]: in-memory monitor registry.
//! - [`api`]: Axum HTTP backend.

pub mod api;
pub mod error;
pub mod evidence;
pub mod kernel;
pub mod language;
pub mod matcher;
pub mod oracle;
pub mod store;
