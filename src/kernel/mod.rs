//! Solver kernel: reduced ordered BDDs with complemented edges.
//!
//! * [`edge`] — the complement-pointer encoding
//! * [`manager`] — the arena, unique table, builder, apply, restrict
//! * [`gc`] — mark-sweep-compact collection preserving declared roots
//! * [`error`] — typed, categorized kernel failures

pub(crate) mod edge;
pub(crate) mod error;
pub(crate) mod gc;
pub(crate) mod manager;

pub(crate) use edge::Edge;
pub use error::{ErrorKind, KernelError};
pub(crate) use manager::NodeView;
pub use manager::{BddManager, GcReport, NodeRef, Op, Stats, VarId};

#[cfg(test)]
mod kernel_tests;
