//! Independent integration test suite.
#![cfg(test)]
//!
//! These tests assert concrete results and concrete failure categories, never
//! merely "the endpoint responds". Reference answers come from:
//!
//! * hand-derived expectations for each fixture (see each test);
//! * `pn_fixtures::oracle::indie`, a semantics implementation that does not
//!   call the kernel firing code at all.
//!
//! The kernel BFS, the kernel-fire oracle and the independent semantics must
//! all agree; any divergence fails the suite.

mod crosscheck;
mod deadlock;
mod firing_law;
mod invariants;
mod mutex;
mod producer_consumer;

mod api;
