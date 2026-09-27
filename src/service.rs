//! Domain service: validation, atomic version publication, history and replay.
//!
//! Concurrency model: a single `RwLock` guards the version vector **and** the
//! append log. Readers take a read lock only long enough to clone an
//! [`Arc<Version>`], then release it; sum computation runs lock-free on the
//! immutable snapshot. Writers:
//! 1. fully validate and construct the new immutable snapshot (nothing visible),
//! 2. `fsync` the commit to the log,
//! 3. push the snapshot — one pointer store, so publication is atomic and
//!    concurrent queries can observe either the old or the new version, never
//!    a partially applied batch.

use crate::errors::AppError;
use crate::index::compress::CoordTable;
use crate::index::grid::DenseGrid;
use crate::model::{Coord, PointUpdate, Rect, VersionId, VersionKind};
use crate::store::{EventPayload, EventRecord, FsStore, StoreError};
use crate::version::Version;
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};

/// Validate a batch against one fixed table+grid and fold it into a new grid.
/// The whole batch is rejected on the first problem — no partial mutation.
///
/// Checks, in order: non-empty → duplicate inside batch → every coordinate
/// registered → every resulting point total stays in `i64`.
fn fold_batch(
    table: &CoordTable,
    base: &DenseGrid,
    updates: &[PointUpdate],
) -> Result<DenseGrid, AppError> {
    if updates.is_empty() {
        return Err(AppError::EmptyBatch);
    }

    // Duplicate-coordinate check inside this batch (sorted index view keeps
    // this independent of input ordering).
    let mut seen: Vec<(Coord, Coord)> = updates.iter().map(|u| (u.x, u.y)).collect();
    seen.sort_unstable();
    if let Some(w) = seen.windows(2).find(|w| w[0] == w[1]) {
        return Err(AppError::DuplicateInBatch(w[0].0, w[0].1));
    }

    // Every coordinate must already be registered. Offline pre-registration
    // means we reject instead of silently inserting a new coordinate.
    for u in updates {
        if table.index_of(u.x, u.y).is_none() {
            return Err(AppError::UnregisteredCoord(u.x, u.y));
        }
    }

    let mut next = base.clone();
    for u in updates {
        let (ix, iy) = table.index_of(u.x, u.y).expect("checked above");
        if next.add(ix, iy, u.delta).is_none() {
            return Err(AppError::Overflow(format!(
                "({},{}): {} + {}",
                u.x,
                u.y,
                base.get(ix, iy),
                u.delta
            )));
        }
    }
    Ok(next)
}

/// Rebuild a grid under a new table, carrying totals of points that remain
/// registered. Point values are copied one-to-one, so no overflow can arise.
fn carry_over(old_table: &CoordTable, old: &DenseGrid, new_table: &CoordTable) -> DenseGrid {
    let (nx, ny) = new_table.dims();
    let mut out = DenseGrid::zeros(nx, ny);
    for (ix, iy, v) in old.cells() {
        if v == 0 {
            continue;
        }
        let x = old_table.xs.coords()[ix];
        let y = old_table.ys.coords()[iy];
        if let Some((jx, jy)) = new_table.index_of(x, y) {
            out.set(jx, jy, v);
        }
    }
    out
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct VersionInfo {
    pub version: VersionId,
    pub kind: VersionKind,
    pub nx: usize,
    pub ny: usize,
    pub total: i128,
    pub published_at_ns: u128,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct QueryOutcome {
    pub version: VersionId,
    pub sum: i128,
    pub empty: bool,
    /// Cutoffs and inclusion–exclusion terms that produced `sum`.
    pub explain: crate::index::fenwick::RectExplain,
}

struct Inner {
    versions: Vec<Arc<Version>>,
    store: FsStore,
}

/// The registered, versioned point-total store.
pub struct Registry {
    data_dir: PathBuf,
    inner: RwLock<Inner>,
}

impl Registry {
    /// Open the filesystem store at `data_dir` and deterministically rebuild
    /// every version from the committed log.
    pub fn open(data_dir: impl AsRef<Path>) -> Result<Self, AppError> {
        let (store, replay) = FsStore::open(data_dir.as_ref()).map_err(persistence)?;
        let versions = Self::replay(replay.records)?;
        Ok(Self {
            data_dir: data_dir.as_ref().to_path_buf(),
            inner: RwLock::new(Inner { versions, store }),
        })
    }

    pub fn data_dir(&self) -> &Path {
        &self.data_dir
    }

    fn replay(records: Vec<EventRecord>) -> Result<Vec<Arc<Version>>, AppError> {
        let mut versions: Vec<Arc<Version>> = Vec::with_capacity(records.len());
        let mut iter = records.into_iter().peekable();

        if let Some(first) = iter.next() {
            let (xs, ys) = match first.payload {
                EventPayload::Registered { xs, ys } => (xs, ys),
                other => {
                    return Err(AppError::Persistence(format!(
                        "first log event must be 'registered', got {}",
                        event_type(&other)
                    )));
                }
            };
            let table = CoordTable::new(xs, ys);
            let grid = DenseGrid::zeros(table.dims().0, table.dims().1);
            versions.push(Version::new(
                first.version,
                VersionKind::Registered,
                table,
                grid,
            ));
        }

        for rec in iter {
            let prev = versions.last().expect("baseline exists").clone();
            let v = match rec.payload {
                EventPayload::Registered { .. } => {
                    return Err(AppError::Persistence(format!(
                        "duplicate 'registered' event at version {}",
                        rec.version
                    )));
                }
                EventPayload::Batch { updates } => {
                    let grid = fold_batch(&prev.table, &prev.grid, &updates)?;
                    Version::new(rec.version, VersionKind::Batch, prev.table.clone(), grid)
                }
                EventPayload::Rebuilt { xs, ys } => {
                    let table = CoordTable::new(xs, ys);
                    if table.xs.is_empty() || table.ys.is_empty() {
                        return Err(AppError::Persistence(format!(
                            "version {} rebuilds an empty axis",
                            rec.version
                        )));
                    }
                    let grid = carry_over(&prev.table, &prev.grid, &table);
                    Version::new(rec.version, VersionKind::Rebuild, table, grid)
                }
            };
            versions.push(v);
        }
        Ok(versions)
    }

    /// Initial registration; allowed only on an empty store.
    pub fn register(
        &self,
        mut xs: Vec<Coord>,
        mut ys: Vec<Coord>,
    ) -> Result<VersionInfo, AppError> {
        xs.sort_unstable();
        xs.dedup();
        ys.sort_unstable();
        ys.dedup();
        if xs.is_empty() {
            return Err(AppError::EmptyAxis("x"));
        }
        if ys.is_empty() {
            return Err(AppError::EmptyAxis("y"));
        }

        let mut inner = self.inner.write().expect("registry lock poisoned");
        if !inner.versions.is_empty() {
            return Err(AppError::BadRequest(
                "coordinates already registered; use /admin/rebuild".to_string(),
            ));
        }
        let table = CoordTable::new(xs.clone(), ys.clone());
        let record = inner
            .store
            .commit(EventPayload::Registered { xs, ys })
            .map_err(persistence)?;
        let grid = DenseGrid::zeros(table.dims().0, table.dims().1);
        let v = Version::new(record.version, VersionKind::Registered, table, grid);
        inner.versions.push(v.clone());
        Ok(info_of(&v))
    }

    /// Apply one atomic increment batch and publish a new version.
    pub fn apply_batch(&self, updates: Vec<PointUpdate>) -> Result<VersionInfo, AppError> {
        let mut inner = self.inner.write().expect("registry lock poisoned");
        let prev = inner
            .versions
            .last()
            .cloned()
            .ok_or(AppError::NotInitialized)?;

        // Full validation + snapshot construction before any durable write.
        let grid = fold_batch(&prev.table, &prev.grid, &updates)?;
        let record = inner
            .store
            .commit(EventPayload::Batch { updates })
            .map_err(persistence)?;
        let v = Version::new(record.version, VersionKind::Batch, prev.table.clone(), grid);
        inner.versions.push(v.clone());
        Ok(info_of(&v))
    }

    /// Rebuild the coordinate tables. Totals of points present in both old and
    /// new tables are carried over; points dropped from the tables leave the
    /// index. Old versions remain fully queryable.
    pub fn rebuild(&self, mut xs: Vec<Coord>, mut ys: Vec<Coord>) -> Result<VersionInfo, AppError> {
        xs.sort_unstable();
        xs.dedup();
        ys.sort_unstable();
        ys.dedup();
        if xs.is_empty() {
            return Err(AppError::EmptyAxis("x"));
        }
        if ys.is_empty() {
            return Err(AppError::EmptyAxis("y"));
        }

        let mut inner = self.inner.write().expect("registry lock poisoned");
        let prev = inner
            .versions
            .last()
            .cloned()
            .ok_or(AppError::NotInitialized)?;

        let table = CoordTable::new(xs.clone(), ys.clone());
        let record = inner
            .store
            .commit(EventPayload::Rebuilt { xs, ys })
            .map_err(persistence)?;
        let grid = carry_over(&prev.table, &prev.grid, &table);
        let v = Version::new(record.version, VersionKind::Rebuild, table, grid);
        inner.versions.push(v.clone());
        Ok(info_of(&v))
    }

    /// Sum an inclusive rectangle on the chosen version (`None` = head).
    pub fn query(&self, at: Option<VersionId>, rect: &Rect) -> Result<QueryOutcome, AppError> {
        let v = {
            let inner = self.inner.read().expect("registry lock poisoned");
            if inner.versions.is_empty() {
                return Err(AppError::NotInitialized);
            }
            let target = at.unwrap_or_else(|| inner.versions.len() as VersionId);
            if target == 0 || target as usize > inner.versions.len() {
                return Err(AppError::UnknownVersion(target));
            }
            inner.versions[(target - 1) as usize].clone()
        };
        let explain = v.rect_explain(rect);
        Ok(QueryOutcome {
            version: v.id,
            sum: explain.sum,
            empty: explain.empty,
            explain,
        })
    }

    pub fn list_versions(&self) -> Vec<VersionInfo> {
        let inner = self.inner.read().expect("registry lock poisoned");
        inner.versions.iter().map(|v| info_of(v)).collect()
    }

    pub fn head_version(&self) -> Option<VersionId> {
        let inner = self.inner.read().expect("registry lock poisoned");
        inner.versions.last().map(|v| v.id)
    }

    /// Read-only view of a version's compression table (for tests/admin).
    pub fn with_head_table<R>(&self, f: impl FnOnce(&CoordTable) -> R) -> Option<R> {
        let inner = self.inner.read().expect("registry lock poisoned");
        inner.versions.last().map(|v| f(&v.table))
    }
}

fn info_of(v: &Version) -> VersionInfo {
    VersionInfo {
        version: v.id,
        kind: v.kind,
        nx: v.table.dims().0,
        ny: v.table.dims().1,
        total: v.grid.total(),
        published_at_ns: v.published_at_ns,
    }
}

fn event_type(p: &EventPayload) -> &'static str {
    match p {
        EventPayload::Registered { .. } => "registered",
        EventPayload::Batch { .. } => "batch",
        EventPayload::Rebuilt { .. } => "rebuilt",
    }
}

fn persistence(e: StoreError) -> AppError {
    AppError::Persistence(e.to_string())
}
