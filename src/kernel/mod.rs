//! Index kernel: hashing, hypergraph peeling, slot assignment, lookup.

pub mod assign;
pub mod builder;
pub mod graph;
pub mod index;

pub use builder::{build, BuildError, BuildOutcome};
pub use index::{Lookup, MphIndex, NotMemberReason};
