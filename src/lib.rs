//! symex-service: symbolic execution for a small fixed-width integer language.
//!
//! Module map (mirrors the required layering):
//! - [`lang`]     — input language: lexer, parser, validator, type inference
//! - [`kernel`]   — solver kernel: symbolic state, SMT translation, path search
//! - [`evidence`] — evidence: independent concrete interpreter, replay, native
//!                  path-condition evaluation used for differential cross-checks
//! - [`api`]      — backend interface: HTTP DTOs, routes, run orchestration
//! - [`config`]   — configuration layer (file + env overrides)

pub mod api;
pub mod config;
pub mod evidence;
pub mod kernel;
pub mod lang;

pub const SERVICE_NAME: &str = "symex-service";
pub const SERVICE_VERSION: &str = env!("CARGO_PKG_VERSION");

/// Z3 runtime version string, e.g. "4.8.12".
pub fn z3_version() -> String {
    let mut major: u32 = 0;
    let mut minor: u32 = 0;
    let mut build: u32 = 0;
    let mut rev: u32 = 0;
    unsafe {
        z3_sys::Z3_get_version(&mut major, &mut minor, &mut build, &mut rev);
    }
    format!("{major}.{minor}.{build}.{rev}")
}
