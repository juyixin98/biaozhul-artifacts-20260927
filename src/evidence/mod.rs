//! Evidence layer — everything here is deliberately independent of the SMT
//! kernel: no Z3 types appear. The concrete interpreter and the native
//! path-condition evaluator share the wrapping semantic primitives in
//! [`sem`], giving tests a second, hand-written oracle to compare the
//! symbolic engine against.

pub mod concrete;
pub mod native;
pub mod replay;
pub mod sem;

pub use concrete::{
    run as run_concrete, ConcreteError, ConcreteInput, ConcreteOutcome, ConcreteResult,
    ConcreteStep, FailureKind, FailureSite,
};
pub use native::{
    eval_pc_over_domain, NBinOp, NBool, NInt, NUnOp, NativeEvalError, SsaEnv,
};
pub use replay::{replay_counterexample, ReplayCheck, ReplayStatus};
