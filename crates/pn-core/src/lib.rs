//! `pn-core`: solving kernel.
//!
//! Owns the mathematical Petri net model: places with explicit finite
//! capacities, weighted arcs, markings, the atomic firing semantics and the
//! bounded state-space (BFS) reachability exploration.
//!
//! This crate deliberately knows nothing about JSON or HTTP. Inputs are
//! already typed; structural validation is still performed defensively so the
//! kernel can never be driven into an inconsistent state by any caller.

pub mod explore;
pub mod fire;
pub mod net;

pub use explore::{
    DeadlockInfo, ExploreConfig, ExploreStats, ExplorationResult, Progress, Target, Verdict,
    WitnessStep,
};
pub use fire::{enabled_transitions, fire, fire_failure_name, FireFailure};
pub use net::{ArcDef, CoreError, Net, PlaceDef, Token, TransitionDef};

/// A marking is one token count per place, indexed by place index in
/// [`Net::place_index`] order.
pub type Marking = Vec<Token>;
