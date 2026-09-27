//! Immutable, shareable snapshot of one published version.
//!
//! A version owns its own compression table, dense point totals and Fenwick
//! index, so coordinate rebuilds never mutate history: old `Version`s keep
//! answering queries after a new table is registered. Readers hold an
//! [`Arc<Version>`]; publication is a single pointer swap in the service
//! layer, which is what makes batch publication atomic (a query can never see
//! half of a batch).

use crate::index::compress::CoordTable;
use crate::index::fenwick::Fenwick2D;
use crate::index::grid::DenseGrid;
use crate::model::{Rect, VersionId, VersionKind};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug)]
pub struct Version {
    pub id: VersionId,
    pub kind: VersionKind,
    pub table: CoordTable,
    pub grid: DenseGrid,
    pub fw: Fenwick2D,
    /// Unix-epoch nanoseconds of publication, recorded for diagnostics.
    pub published_at_ns: u128,
}

impl Version {
    pub fn new(id: VersionId, kind: VersionKind, table: CoordTable, grid: DenseGrid) -> Arc<Self> {
        let fw = Fenwick2D::from_grid(&grid);
        Arc::new(Self {
            id,
            kind,
            table,
            grid,
            fw,
            published_at_ns: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("system clock before Unix epoch")
                .as_nanos(),
        })
    }

    /// Sum inside the inclusive rectangle on this version.
    pub fn rect_sum(&self, rect: &Rect) -> i128 {
        self.rect_explain(rect).sum
    }

    /// Sum plus the full cutoff / inclusion–exclusion breakdown.
    pub fn rect_explain(&self, rect: &Rect) -> crate::index::fenwick::RectExplain {
        if rect.is_empty() {
            return crate::index::fenwick::RectExplain {
                empty: true,
                cutoffs: crate::index::fenwick::Cutoffs {
                    x_le_hi: 0,
                    x_lt_lo: 0,
                    y_le_hi: 0,
                    y_lt_lo: 0,
                },
                terms: crate::index::fenwick::Terms {
                    hh: 0,
                    lh: 0,
                    hl: 0,
                    ll: 0,
                },
                sum: 0,
            };
        }
        crate::index::fenwick::RectQuery::new(
            &self.fw,
            &self.table,
            rect.x_lo,
            rect.x_hi,
            rect.y_lo,
            rect.y_hi,
        )
        .explain()
    }
}
