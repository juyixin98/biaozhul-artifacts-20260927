//! # se-lang
//!
//! Input language definition for the symbolic-execution service:
//!
//! * [`ast`] — the lowered, validated program representation used by every other crate.
//! * [`dto`] — serde JSON schema, parsing and validation (`ProgramDto::parse`).
//! * [`bits`] — fixed-width (8/16/32/64 bit) arithmetic with explicit wrap semantics.
//! * [`interp`] — a fully **independent concrete interpreter**. It shares only the AST
//!   with the symbolic engine; its evaluation rules are implemented independently and
//!   are used both for counterexample replay (evidence verification) and for the
//!   small-domain exhaustive oracle.
//!
//! Nothing in this crate performs any symbolic reasoning; it is the reference semantics.

pub mod ast;
pub mod bits;
pub mod dto;
pub mod interp;
pub mod util;

pub use ast::{BinOp, Block, Expr, OverflowMode, Program, Stmt, UnOp, Width};
pub use dto::ProgramDto;
pub use interp::{Failure, FailureKind, FlowOutcome, RunOpts, RunResult};
