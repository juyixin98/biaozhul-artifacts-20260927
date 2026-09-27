//! Independent blackbox tests.
//!
//! Oracles live in `fsm-fixtures::answers` and were derived by hand; this
//! crate never trusts a number produced by the kernel it tests.
#![cfg(test)]

mod helpers;

mod api_tests;
mod budget_tests;
mod counter_tests;
mod mutex_tests;
mod swap_tests;
mod verify_tests;
