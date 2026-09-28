//! # se-solver
//!
//! Solver kernel for the symbolic-execution service:
//!
//! * [`term`] — the engine-facing symbolic term language (bitvectors + booleans).
//! * [`smt`] — lowering to SMT-LIB 2 (QF_BV).
//! * [`solver`] — the [`solver::SmtSolver`] trait plus a backend driving the mature
//!   Z3 theorem prover through its command-line SMT-LIB interface.
//!
//! Conservatism is a design rule: anything short of an explicit solver verdict yields
//! [`solver::CheckStatus::Unknown`].

pub mod smt;
pub mod solver;
pub mod term;

pub use solver::{
    CheckResult, CheckStatus, Model, SmtSolver, SolverError, Z3Cli,
};
pub use term::{
    and, bv, bv_bin, bv_un, boolc, implies, ite, nonzero, not, or, rel, var,
    BoolBinOp, BoolUnOp, BvBinOp, BvUnOp, CmpRel, Sort, Term, TermNode,
};
