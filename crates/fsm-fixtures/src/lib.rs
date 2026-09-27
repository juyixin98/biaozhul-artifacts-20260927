//! # fsm-fixtures
//!
//! Local, synthetic specifications shared by the CLI, the HTTP layer and the
//! independent test suite. Each fixture ships with its expected answers in
//! [`answers`], but those answers are written here as hand-derived constants
//! (state counts, path lengths, verdict categories) — they are **not**
//! computed by calling `fsm-core`, so the test suite has an independent
//! oracle.

pub mod answers;
pub mod specs;

pub use specs::*;
