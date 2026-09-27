//! # bdd-backend
//!
//! A reduced ordered binary decision diagram (ROBDD) backend for Boolean
//! functions, organized into modules with real responsibilities:
//!
//! * [`lang`] — the Boolean input language (lexer, parser, AST);
//! * [`kernel`] — the solver core: fixed variable order, unique table with
//!   redundant-node elimination, uniform complement-edge negation, `apply`,
//!   `restrict`, cross-manager/stale-reference checks, and root-preserving
//!   garbage collection;
//! * [`oracle`] — an **independent** truth-table interpreter used to verify
//!   the kernel (it never touches a BDD);
//! * [`verify`] — equivalence evidence combining structural isomorphism with
//!   exhaustive oracle checking, witnesses, and accept/reject/inconclusive
//!   decisions;
//! * [`api`] — the Axum HTTP surface;
//! * [`diag`] / [`config`] — correlation-id diagnostics with redaction, and
//!   layered runtime configuration.

pub mod api;
pub mod config;
pub mod diag;
pub mod kernel;
pub mod lang;
pub mod oracle;
pub mod verify;

pub use config::Config;
