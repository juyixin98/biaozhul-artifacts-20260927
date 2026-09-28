//! 核心求解内核：网模型、发射语义、可达性搜索与 P 不变量。

pub mod fire;
pub mod invariants;
pub mod model;
pub mod reach;

pub use fire::{fire, is_enabled, why_not_enabled, FireBlocked, KernelError};
pub use invariants::{
    compute_invariants, conservation_residual, weighted_sum, InvariantBounds, PInvariant,
    PInvariantReport,
};
pub use model::{ArcExpr, Marking, Net, Place, PlaceId, Transition, TransitionId};
pub use reach::{
    analyze_reachability, state_space_upper_bound, ReachabilityDecision, ReachabilityOptions,
    ReachabilityResult, ReachedCertificate, SearchProgress, InvariantObstruction,
};
