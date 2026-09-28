//! # fsm-lang
//!
//! Declarative input language for the local explicit-state FSM model
//! checker: specification model, compilation (name/type resolution),
//! domain enumeration, expression semantics, canonical fingerprint and the
//! shared evidence contract.

pub mod compile;
pub mod error;
pub mod evidence;
pub mod fingerprint;
pub mod model;

pub use compile::{CompiledSpec, DomainIter, RExpr, RProperty, RTransition, RUpdate, Type};
pub use error::{EvalError, LangError, StateError};
pub use model::{
    Domain, Expr, Property, PropertyKind, Spec, Transition, Update, Value, Variable,
};
