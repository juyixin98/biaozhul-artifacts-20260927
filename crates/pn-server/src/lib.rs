//! `pn-server`: Axum backend exposing bounded-capacity weighted Petri net
//! reachability analysis.
//!
//! Modules: configuration ([`config`]), per-request run identity ([`runid`]),
//! HTTP routing ([`http`]) and wire DTOs ([`dto`]). The binary is a thin
//! wrapper in `src/main.rs`.

pub mod config;
pub mod dto;
pub mod http;
pub mod runid;
