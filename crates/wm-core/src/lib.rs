//! `wm-core`: Wavelet matrix index kernel.
//!
//! Semantics (contract honored by every module of the workspace):
//!
//! * Elements are `i64`. Coordinate compression is **order preserving**:
//!   each distinct value is mapped to the number of distinct values smaller
//!   than it, so rank order equals signed integer order.
//! * All ranges are **half-open** `[l, r)` with `0 <= l <= r <= n`.
//! * `k` of the range k-th-smallest query is **0-based**
//!   (`0 <= k < r - l`).
//! * Queries are answered by the wavelet matrix navigation algorithm over
//!   block-ranked bit-vectors. They never materialize or sort slices.

pub mod bitvec;
pub mod error;
pub mod wavelet;

pub use bitvec::BitVector;
pub use error::WmError;
pub use wavelet::WaveletMatrix;
