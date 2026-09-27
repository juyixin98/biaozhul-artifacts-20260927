//! Library surface of the backend: DTOs, service pipeline and HTTP router.
pub mod config;
pub mod dto;
pub mod http;
pub mod service;

pub use service::{
    new_request_id, run_analyze, run_analyze_with, run_verify, run_verify_with,
};
