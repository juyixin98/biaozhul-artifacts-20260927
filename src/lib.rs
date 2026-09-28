//! # diffconstraints
//!
//! Integer difference-constraint service: named constraints of the form
//! `x - y <= c`, feasibility solving, and negative-cycle conflict evidence.
//!
//! Layered architecture (each module has one job):
//!
//! | module | responsibility |
//! |---|---|
//! | [`error`] | the single error-kind contract (`input` / `state_conflict` / `resource_exhausted` / `computation_failed` / `not_found`) |
//! | [`model`] | validated canonical [`model::Constraint`] |
//! | [`lang`] | small text input language (`c1: x - y <= 5`) |
//! | [`graph`] | constraint → weighted directed edge reduction, connected components |
//! | [`solver`] | Bellman–Ford kernel with checked arithmetic and replayable traces |
//! | [`evidence`] | independent verification of assignments and conflict cycles |
//! | [`store`] | named-constraint state with atomic batch transactions |
//! | [`service`] | orchestration: store → graph → solver → evidence |
//! | [`http_api`] | axum JSON boundary and error mapping |
//! | [`testlog`] | structured, replayable per-run test logs |

pub mod error;
pub mod evidence;
pub mod graph;
pub mod http_api;
pub mod lang;
pub mod model;
pub mod service;
pub mod solver;
pub mod store;
pub mod testlog;

pub use error::{ErrorKind, ServiceError, ServiceResult};
pub use model::Constraint;
pub use service::ConstraintService;
pub use store::{BatchOp, OpResult};
