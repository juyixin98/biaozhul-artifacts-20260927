//! Hand-derived expected answers for the fixtures.
//!
//! These constants were derived by reasoning about each fixture, NOT by
//! running `fsm-core`. The blackbox tests compare the kernel against them,
//! giving an independent oracle. They also encode the concrete replay of the
//! mutex counterexample step by step.

/// Expected conclusions for one property on one fixture.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Expected {
    Holds,
    Violated,
    Unknown,
    /// Run-level error expected instead of per-property conclusions.
    RunError(&'static str),
}

// ---- mutex_safe -----------------------------------------------------------
pub const MUTEX_SAFE_REACHABLE_STATES: u64 = 3;
pub const MUTEX_SAFE_INVARIANT: Expected = Expected::Holds;
pub const MUTEX_SAFE_DEADLOCK: Expected = Expected::Holds;
pub const MUTEX_SAFE_TERMINAL_STATES: u64 = 0;

// ---- mutex_bad ------------------------------------------------------------
// Reachability (hand-derived; note leave updates only two variables, so the
// third keeps its value):
//   FFF --t2_enter--> FTT ; FFF --t1_enter--> TFT
//   FTT --t1_enter--> TTT (violation); FTT --t2_leave--> FTF
//   FTF --t1_enter--> TTF (violation); TTF --t2_enter--> TTT
// => reachable = {FFF, FTT, TFT, FTF, TTF, TTT} = 6 states.
pub const MUTEX_BAD_REACHABLE_STATES: u64 = 6;
pub const MUTEX_BAD_INVARIANT: Expected = Expected::Violated;
/// Shortest violating path length (transitions).
pub const MUTEX_BAD_CEX_LENGTH: usize = 2;

/// Hand replay of the shortest mutex counterexample.
/// `(in1, in2, locked)` plus the transition fired to arrive.
pub const MUTEX_BAD_CEX: &[([&str; 3], Option<&str>)] = &[
    (["false", "false", "false"], None),
    (["false", "true", "true"], Some("t2_enter")),
    (["true", "true", "true"], Some("t1_enter")),
];

// ---- counter --------------------------------------------------------------
pub const COUNTER_REACHABLE_STATES: u64 = 4;
pub const COUNTER_TERMINAL_STATES: u64 = 1;
pub const COUNTER_DEADLOCKED_STATES: u64 = 0;
pub const COUNTER_INVARIANT_EXPECTED: Expected = Expected::Holds;
pub const COUNTER_UNREACHABLE_EXPECTED: Expected = Expected::Holds;
pub const COUNTER_CAP_EXPECTED: Expected = Expected::Violated;
pub const COUNTER_CAP_PATH_LENGTH: usize = 3;

// ---- counter_deadlock -----------------------------------------------------
pub const COUNTER_DL_REACHABLE_STATES: u64 = 4;
pub const COUNTER_DL_TERMINAL_STATES: u64 = 0;
pub const COUNTER_DL_DEADLOCKED_STATES: u64 = 1;
pub const COUNTER_DL_PATH_LENGTH: usize = 3;

// ---- no_init --------------------------------------------------------------
pub const NO_INIT_ERROR: &str = "no_initial_state";

// ---- big_counter ----------------------------------------------------------
/// Budget smaller than the 5001-state space.
pub const BIG_BUDGET: u64 = 500;
pub const BIG_EXPECTED_AG: Expected = Expected::Unknown;
pub const BIG_EXPECTED_EF_FAR: Expected = Expected::Unknown;
/// A target within the truncated prefix must still be found with a witness.
pub const BIG_EF_NEAR_TARGET: &str = "x == 10";
pub const BIG_EF_NEAR_EXPECTED: Expected = Expected::Violated;
pub const BIG_EF_NEAR_LENGTH: usize = 10;

// ---- swap -----------------------------------------------------------------
pub const SWAP_REACHABLE_STATES: u64 = 2;
pub const SWAP_EXPECTED: Expected = Expected::Violated;
pub const SWAP_PATH_LENGTH: usize = 1;
/// Post-state of the single swap: parallel assignment gives (x=2, y=1).
pub const SWAP_AFTER: (i64, i64) = (2, 1);
