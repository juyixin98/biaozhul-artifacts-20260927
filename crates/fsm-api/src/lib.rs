//! HTTP/CLI backend crate for the explicit FSM checker.

pub mod cli;
pub mod config;
pub mod middleware;
pub mod routes;
pub mod service;
pub mod types;

pub use config::Config;
pub use routes::app;
pub use service::ServiceState;
