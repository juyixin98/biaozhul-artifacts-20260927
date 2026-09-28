//! `pn-verify`: independent evidence verification.
//!
//! Everything here re-derives verdicts from first principles instead of
//! trusting the solver. A witness path is replayed firing-by-firing against
//! the kernel primitives; an invariant is checked directly against the net's
//! arc definitions; a deadlock claim is tested by attempting every
//! transition. None of these routines call the BFS solver, so a bug in the
//! solver cannot also forge its own proof.

pub mod deadlock;
pub mod invariant;
pub mod witness;

pub use deadlock::{verify_deadlock, DeadlockVerdict};
pub use invariant::{verify_invariant_vector, InvariantVerdict};
pub use witness::{
    verify_witness, StepClaim, VerifiedStep, VerifiedWitness, VerifyFailure, VerifyFailureKind,
};
