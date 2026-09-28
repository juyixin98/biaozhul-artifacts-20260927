//! `pn-fixtures`: local synthetic test data and an independent oracle.
//!
//! Three standard nets are provided both as typed `pn_core::Net` objects and
//! as `petri-analysis/v1` JSON:
//!
//! * [`nets::mutex`]            - mutually exclusive shared resource;
//! * [`nets::producer_consumer`]- bounded-buffer producer/consumer;
//! * [`nets::deadlock_net`]     - a net engineered to reach a terminal marking.
//!
//! [`oracle`] contains a deliberately simple, dependency-light brute-force
//! enumerator used as an independent reference answer. It does not call the
//! solver crate and is written differently from the kernel BFS on purpose.

pub mod nets;
pub mod oracle;
