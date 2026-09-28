//! # fsm-evidence
//!
//! Independent trace replay and evidence verification. Does not depend on
//! `fsm-core`: it re-derives every step from the compiled specification and
//! the serialized evidence, so kernel bugs cannot validate their own output.

pub mod replay;

pub use replay::{replay, ReplayFailure, ReplayReport, ReplayStep};
