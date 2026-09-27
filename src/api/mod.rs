//! Backend interface: HTTP DTOs, routes, and run orchestration.

pub mod dto;
pub mod routes;

pub use routes::build_router;
