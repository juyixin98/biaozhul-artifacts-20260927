//! # prereg2d
//!
//! Offline backend for 2D point increments and inclusive-rectangle sum
//! queries over **pre-registered** coordinates.
//!
//! Layer layout:
//! * [`model`] — wire/on-disk data format and boundary semantics
//! * [`index`] — index kernel: compression, dense totals, 2D Fenwick
//! * [`store`] — filesystem persistence adapter (append log + `CURRENT`)
//! * [`service`] — validation, atomic version publication, replay
//! * [`api`] — Axum verification interface
//! * [`config`] — file + environment configuration

pub mod api;
pub mod config;
pub mod errors;
pub mod index;
pub mod model;
pub mod service;
pub mod store;
pub mod version;
