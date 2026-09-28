//! Evidence verification.
//!
//! The symbolic engine's candidates are **not trusted**. Every reported counterexample
//! is replayed through the independent concrete interpreter in `se-lang`:
//!
//! * the replay must fail,
//! * with the same failure category,
//! * at the same statement id,
//! * and the input assignment is normalized into the declared domains.
//!
//! Only fully corroborated candidates are marked `confirmed`. Everything else is
//! `rejected` with a reason, and a rejected witness prevents the public verdict from
//! claiming `violation`.
//!
//! The module also provides [`oracle::exhaustive_oracle`], a small-domain brute-force
//! ground truth computed without any SMT involvement, used by the independent test
//! layer to compare against engine results.

pub mod oracle;
pub mod replay;

pub use oracle::{OracleFailure, OracleResult, OracleSummary};
pub use replay::{verify_evidence, verify_report, VerifiedEvidence, VerifiedReport};
