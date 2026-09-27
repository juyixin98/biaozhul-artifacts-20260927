//! # fsm-lang
//!
//! Input language for the explicit-state model checker.
//!
//! A specification declares:
//! * finite-ranged state variables (boolean, bounded integer range, or enum),
//! * an initial predicate (or an explicit concrete state),
//! * a set of named transitions, each with a boolean guard and a parallel
//!   assignment: every right-hand side is evaluated against the **same
//!   pre-state**, then all assignments are committed together,
//! * an optional terminal (legal-termination) predicate.
//!
//! Two input formats are accepted and share one typed AST:
//! * a small textual DSL (see [`parser::parse_system`]),
//! * a JSON document (see [`json::system_from_json`]).

pub mod ast;
pub mod codec;
pub mod error;
pub mod eval;
pub mod json;
pub mod lexer;
pub mod parser;
pub mod semantics;
pub mod system;
pub mod token;

pub use ast::{Assign, Expr, RawSystem, RawTransition};
pub use error::{BuildError, BuildErrorKind};
pub use eval::{build_system, check_boolean_predicate, EvalError, EvalErrorKind};
pub use semantics::render_value;
pub use system::{Domain, System, Transition, Value, Var};

/// Crate version, surfaced by the API for "version / processing location"
/// explainability.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
