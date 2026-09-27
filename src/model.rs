//! Data-format layer: the wire and on-disk value types shared by every layer.
//!
//! Boundary semantics (documented here and in `docs/SEMANTICS.md`):
//! * Point coordinates are `i64`.
//! * Rectangle boundaries are [`i128`], **inclusive on both sides**
//!   (`x_lo <= x <= x_hi` AND `y_lo <= y <= y_hi`). The wider type lets
//!   callers address the extreme points `i64::MIN` / `i64::MAX` without any
//!   overflow and without an awkward half-open special case.
//! * A rectangle with `x_lo > x_hi` or `y_lo > y_hi` is an **empty** rectangle
//!   and every empty rectangle sums to `0` (it is not an error).
//! * Point values/updates are `i64`; accumulated point totals must stay within
//!   `i64`, otherwise the batch is rejected with `overflow`.

use serde::{Deserialize, Serialize};

/// A registered point coordinate (one axis value).
pub type Coord = i64;
/// A point weight / delta weight.
pub type Weight = i64;
/// Published-version identifier, monotonically increasing from 1.
pub type VersionId = u64;

/// One increment in a batch.
///
/// Repeating the same coordinate twice *within one batch* is rejected
/// (`duplicate_in_batch`): callers must fold their deltas beforehand. This
/// keeps the batch's intent unambiguous and the overflow check exact.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct PointUpdate {
    pub x: Coord,
    pub y: Coord,
    pub delta: Weight,
}

/// Inclusive rectangle `[x_lo, x_hi] × [y_lo, y_hi]`.
///
/// Fields are [`i128`] in the JSON wire format too; serde_json accepts plain
/// integer literals (including magnitudes beyond `i64`) for them.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Rect {
    pub x_lo: i128,
    pub x_hi: i128,
    pub y_lo: i128,
    pub y_hi: i128,
}

impl Rect {
    /// True iff the rectangle contains no points.
    pub fn is_empty(&self) -> bool {
        self.x_lo > self.x_hi || self.y_lo > self.y_hi
    }

    /// Inclusive membership for a registered point.
    pub fn contains(&self, x: Coord, y: Coord) -> bool {
        let (x, y) = (x as i128, y as i128);
        self.x_lo <= x && x <= self.x_hi && self.y_lo <= y && y <= self.y_hi
    }
}

/// Kind label carried by every published version (also persisted).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum VersionKind {
    /// Baseline version produced by the initial coordinate registration.
    Registered,
    /// Version produced by an incremental point batch.
    Batch,
    /// Version produced by re-registering (rebuilding) the coordinate tables.
    Rebuild,
}
