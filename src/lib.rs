//! wtio — weak trace inclusion checker for finite labeled transition systems.
//!
//! Crate layout (each module owns one engineering boundary):
//!
//! | module       | boundary |
//! |--------------|----------|
//! | [`input`]    | JSON input language + request shapes |
//! | [`compiler`] | name interning, validation, explicit alphabet alignment |
//! | [`model`]    | dense solver-side representation |
//! | [`closure`]  | tau closures with replayable BFS parent mappings |
//! | [`solver`]   | determinized BFS inclusion kernel |
//! | [`witness`]  | independent concrete accepting-run reconstruction |
//! | [`verifier`] | independent evidence replay + acceptance cross-check |
//! | [`oracle`]   | brute-force enumeration oracle (test support) |
//! | [`engine`]   | orchestration + shared data/error contract |
//! | [`api`]      | Axum HTTP backend |
//! | [`diagnostics`] | run ids and replayable structured run logs |

pub mod api;
pub mod closure;
pub mod compiler;
pub mod diagnostics;
pub mod engine;
pub mod error;
pub mod input;
pub mod model;
pub mod oracle;
pub mod solver;
pub mod verifier;
pub mod witness;
