//! 版本化存储内核：注册冻结、批更新原子发布、任意历史版本查询、启动 WAL 重放。
//!
//! 并发模型：`RwLock<Inner>`。提交在**读阶段**基于不可变历史快照计算新版本，
//! 进入写锁后做一次 base 版本复核（乐观并发），通过则 `WAL.append`（fsync）
//! 再压入快照。任何校验失败都发生在发布之前，查询永远见不到半批。
//!
//! 版本号是“表内”的：每个表从基线版本 0 开始，每批 +1；历史快照全部保留。
//! 全局物理事件序号（WAL seq）与表内版本号是两个概念。

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::RwLock;

use crate::coord::CoordinateTable;
use crate::error::{CoreError, CoreResult};
use crate::fenwick::Fenwick2d;
use crate::model::{PointUpdate, VersionInfo};
use crate::persist::{BatchCommitted, Event, EventLog, StoredPoint, TableRegistered};
use crate::rect::{Rect, Selection};

/// 可注入时钟（测试固定时间，生产为墙钟毫秒）。
pub trait Clock: Send + Sync {
    fn now_ms(&self) -> i64;
}

pub struct SystemClock;

impl Clock for SystemClock {
    fn now_ms(&self) -> i64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0)
    }
}

/// 某一版本的完整不可变快照。
#[derive(Debug, Clone)]
pub struct Snapshot {
    pub info: VersionInfo,
    /// 行优先压缩格点值（i64；提交前已保证逐点不溢出）。
    values: Vec<i64>,
    bit: Fenwick2d,
}

pub struct TableState {
    pub id: u32,
    pub table: CoordinateTable,
    /// 下标即表内版本号；snapshots[0] 为基线。
    snapshots: Vec<Snapshot>,
}

impl TableState {
    pub fn latest_version(&self) -> u64 {
        (self.snapshots.len() - 1) as u64
    }

    pub fn snapshot(&self, version: u64) -> Option<&Snapshot> {
        self.snapshots.get(version as usize)
    }

    pub fn versions(&self) -> Vec<VersionInfo> {
        self.snapshots.iter().map(|s| s.info).collect()
    }
}

struct Inner {
    tables: BTreeMap<u32, TableState>,
    next_table_id: u32,
    log: EventLog,
}

pub struct Store {
    inner: RwLock<Inner>,
    clock: Box<dyn Clock>,
}

impl std::fmt::Debug for Store {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let g = self.inner.read().unwrap();
        f.debug_struct("Store")
            .field("tables", &g.tables.keys().collect::<Vec<_>>())
            .field("next_table_id", &g.next_table_id)
            .finish()
    }
}

/// 注册结果。
#[derive(Debug, Clone, Copy)]
pub struct RegisterOutcome {
    pub table_id: u32,
    pub version: u64,
    pub nx: usize,
    pub ny: usize,
    pub duplicate_x: usize,
    pub duplicate_y: usize,
    pub created_at_ms: i64,
}

/// 提交结果（新版本元信息）。
#[derive(Debug, Clone, Copy)]
pub struct CommitOutcome {
    pub table_id: u32,
    pub version: u64,
    pub base_version: u64,
    pub update_count: usize,
    pub created_at_ms: i64,
}

/// 查询结果。
#[derive(Debug, Clone, Copy)]
pub struct QueryOutcome {
    pub table_id: u32,
    pub version: u64,
    pub sum: i64,
    /// 矩形内没有任何已注册格点（合法空矩形）。
    pub empty: bool,
    pub selected_x: usize,
    pub selected_y: usize,
}

/// 计算阶段产出的新版本（进入写锁前完成，便于原子发布）。
struct ComputedBatch {
    cells: Vec<StoredPoint>,
    values: Vec<i64>,
    bit: Fenwick2d,
    update_count: usize,
}

impl Store {
    /// 打开数据目录并重放 WAL；日志损坏直接报错，绝不以空状态静默启动。
    pub fn open(dir: &Path) -> CoreResult<Store> {
        Self::open_with_clock(dir, Box::new(SystemClock))
    }

    pub fn open_with_clock(dir: &Path, clock: Box<dyn Clock>) -> CoreResult<Store> {
        let mut log = EventLog::open(dir)?;
        let events = log.read_all()?;
        let mut inner = Inner {
            tables: BTreeMap::new(),
            next_table_id: 1,
            log,
        };
        for (seq, ev) in events {
            inner.apply_replayed(seq, ev)?;
        }
        Ok(Store {
            inner: RwLock::new(inner),
            clock,
        })
    }

    // ---------- 注册 ----------

    pub fn register_table(&self, raw_x: Vec<i64>, raw_y: Vec<i64>) -> CoreResult<RegisterOutcome> {
        let (table, stats) = CoordinateTable::from_coords(raw_x.clone(), raw_y.clone())?;
        let now = self.clock.now_ms();

        let mut g = self.inner.write().unwrap();
        let table_id = g.next_table_id;
        let event = Event::TableRegistered(TableRegistered {
            table_id,
            xs: raw_x,
            ys: raw_y,
            created_at_ms: now,
        });
        // 先持久化（fsync），成功后才在内存发布。
        g.log.append(&event)?;

        let nx = table.nx();
        let ny = table.ny();
        let baseline = Snapshot {
            info: VersionInfo::baseline(now),
            values: vec![0; nx * ny],
            bit: Fenwick2d::zeros(nx, ny),
        };
        g.tables.insert(
            table_id,
            TableState {
                id: table_id,
                table,
                snapshots: vec![baseline],
            },
        );
        g.next_table_id += 1;
        Ok(RegisterOutcome {
            table_id,
            version: 0,
            nx,
            ny,
            duplicate_x: stats.duplicate_x,
            duplicate_y: stats.duplicate_y,
            created_at_ms: now,
        })
    }

    // ---------- 批更新 ----------

    /// `expected_base = None` 表示“必须基于当前最新版本”。
    pub fn commit_batch(
        &self,
        table_id: u32,
        expected_base: Option<u64>,
        updates: Vec<PointUpdate>,
    ) -> CoreResult<CommitOutcome> {
        if updates.is_empty() {
            return Err(CoreError::EmptyBatch);
        }

        // 读阶段：定位表与 base 快照（快照不可变，锁释放后仍可安全计算）。
        let computed;
        let base;
        {
            let g = self.inner.read().unwrap();
            let ts = g
                .tables
                .get(&table_id)
                .ok_or(CoreError::TableNotFound { table: table_id })?;
            let current = ts.latest_version();
            base = match expected_base {
                None => current,
                Some(b) if b <= current => b,
                Some(b) => {
                    return Err(CoreError::VersionNotFound {
                        table: table_id,
                        version: b as i64,
                    })
                }
            };
            if let Some(want) = expected_base {
                if want != current {
                    return Err(CoreError::StaleBaseVersion {
                        table: table_id,
                        expected_base: want,
                        current,
                    });
                }
            }
            let snap = ts
                .snapshot(base)
                .expect("base snapshot exists after bounds check");
            computed = Self::compute_batch(&ts.table, snap, &updates)?;
        }

        let now = self.clock.now_ms();

        // 写阶段：复核 base 未被并发推进 → 持久化 → 发布。
        let mut g = self.inner.write().unwrap();
        if !g.tables.contains_key(&table_id) {
            return Err(CoreError::TableNotFound { table: table_id });
        }
        let current = g.tables[&table_id].latest_version();
        if current != base {
            return Err(CoreError::StaleBaseVersion {
                table: table_id,
                expected_base: base,
                current,
            });
        }
        let new_version = base + 1;
        let update_count = computed.update_count;
        let event = Event::BatchCommitted(BatchCommitted {
            table_id,
            version: new_version,
            base_version: base,
            updates,
            cells: computed.cells,
            created_at_ms: now,
        });
        g.log.append(&event)?;

        let info = VersionInfo {
            version: new_version,
            update_count,
            created_at_ms: now,
            base_version: base,
        };
        g.tables
            .get_mut(&table_id)
            .expect("table presence re-checked above")
            .snapshots
            .push(Snapshot {
                info,
                values: computed.values,
                bit: computed.bit,
            });
        Ok(CommitOutcome {
            table_id,
            version: new_version,
            base_version: base,
            update_count,
            created_at_ms: now,
        })
    }

    /// 纯计算 + 全量校验。任何错误都在此发生，调用方尚未产生任何副作用。
    fn compute_batch(
        table: &CoordinateTable,
        base_snap: &Snapshot,
        updates: &[PointUpdate],
    ) -> CoreResult<ComputedBatch> {
        let nx = table.nx();
        let ny = table.ny();

        // 校验 1：每个坐标必须已注册。按输入顺序报告第一个违规点，
        // 不做任何“最近邻”插入。
        let mut indexed: Vec<(usize, usize, i64)> = Vec::with_capacity(updates.len());
        for u in updates {
            match table.cell_of(u.x, u.y) {
                Some((ix, iy)) => indexed.push((ix, iy, u.delta)),
                None => return Err(CoreError::CoordinateNotRegistered { x: u.x, y: u.y }),
            }
        }

        // 同批内同点先聚合（i128，批内聚合本身可离开 i64）。
        // 用与版本无关的稠密累加数组，再按格点顺序产出，保证溢出报告确定性。
        let mut agg = vec![0i128; nx * ny];
        for (ix, iy, d) in &indexed {
            agg[ix * ny + iy] += *d as i128;
        }

        // 校验 2：逐点累计不得离开 i64。负值允许，只有累计溢出拒绝整批。
        let mut cells = Vec::new();
        let mut values = base_snap.values.clone();
        let mut bit = base_snap.bit.clone();
        for ix in 0..nx {
            for iy in 0..ny {
                let d128 = agg[ix * ny + iy];
                if d128 == 0 {
                    continue;
                }
                let prev = values[ix * ny + iy];
                let nv = prev as i128 + d128;
                if nv < i64::MIN as i128 || nv > i64::MAX as i128 {
                    // 回查原始坐标，让错误携带可复现的输入坐标。
                    return Err(CoreError::PointOverflow {
                        x: table.xs.values()[ix],
                        y: table.ys.values()[iy],
                        previous: prev,
                        batch_delta: d128,
                    });
                }
                let nv = nv as i64;
                values[ix * ny + iy] = nv;
                bit.add(ix, iy, d128);
                cells.push(StoredPoint {
                    ix,
                    iy,
                    delta: d128 as i64,
                });
            }
        }

        Ok(ComputedBatch {
            cells,
            values,
            bit,
            update_count: updates.len(),
        })
    }

    // ---------- 查询 ----------

    pub fn query(
        &self,
        table_id: u32,
        version: Option<u64>,
        rect: &Rect,
    ) -> CoreResult<QueryOutcome> {
        rect.validate()?;
        let g = self.inner.read().unwrap();
        let ts = g
            .tables
            .get(&table_id)
            .ok_or(CoreError::TableNotFound { table: table_id })?;
        let v = version.unwrap_or_else(|| ts.latest_version());
        let snap = ts.snapshot(v).ok_or(CoreError::VersionNotFound {
            table: table_id,
            version: v as i64,
        })?;
        let sel: Selection = rect.select(&ts.table);
        let selected_x = sel.x_end - sel.x_start;
        let selected_y = sel.y_end - sel.y_start;
        if sel.is_empty() {
            return Ok(QueryOutcome {
                table_id,
                version: v,
                sum: 0,
                empty: true,
                selected_x,
                selected_y,
            });
        }
        let raw = sel.sum(&snap.bit);
        if raw < i64::MIN as i128 || raw > i64::MAX as i128 {
            return Err(CoreError::SumOverflow);
        }
        Ok(QueryOutcome {
            table_id,
            version: v,
            sum: raw as i64,
            empty: false,
            selected_x,
            selected_y,
        })
    }

    // ---------- 元信息 ----------

    pub fn list_table_ids(&self) -> Vec<u32> {
        self.inner.read().unwrap().tables.keys().copied().collect()
    }

    pub fn with_table<R>(&self, table_id: u32, f: impl FnOnce(&TableState) -> R) -> CoreResult<R> {
        let g = self.inner.read().unwrap();
        let ts = g
            .tables
            .get(&table_id)
            .ok_or(CoreError::TableNotFound { table: table_id })?;
        Ok(f(ts))
    }

    // ---------- 重放 ----------
}

impl Inner {
    fn apply_replayed(&mut self, seq: u64, ev: Event) -> CoreResult<()> {
        // 重放走与在线提交相同的计算/校验路径，任何不一致都判 CORRUPT_LOG。
        let corrupt = |detail: String| CoreError::CorruptLog {
            offset: 0,
            detail: format!("seq {seq}: {detail}"),
        };
        let g = self;
        match ev {
            Event::TableRegistered(e) => {
                if e.table_id != g.next_table_id {
                    return Err(corrupt(format!(
                        "table_id {} out of order, expected {}",
                        e.table_id, g.next_table_id
                    )));
                }
                let (table, _stats) = CoordinateTable::from_coords(e.xs, e.ys)
                    .map_err(|ce| corrupt(format!("invalid registered coordinates: {ce}")))?;
                let nx = table.nx();
                let ny = table.ny();
                g.tables.insert(
                    e.table_id,
                    TableState {
                        id: e.table_id,
                        table,
                        snapshots: vec![Snapshot {
                            info: VersionInfo::baseline(e.created_at_ms),
                            values: vec![0; nx * ny],
                            bit: Fenwick2d::zeros(nx, ny),
                        }],
                    },
                );
                g.next_table_id += 1;
            }
            Event::BatchCommitted(e) => {
                let ts = g
                    .tables
                    .get_mut(&e.table_id)
                    .ok_or_else(|| corrupt(format!("batch for unknown table {}", e.table_id)))?;
                let current = ts.latest_version();
                if e.version != current + 1 || e.base_version != current {
                    return Err(corrupt(format!(
                        "version chain broken: event v{} base {}, current v{}",
                        e.version, e.base_version, current
                    )));
                }
                let base_snap = ts.snapshots[current as usize].clone();
                let computed = Store::compute_batch(&ts.table, &base_snap, &e.updates)
                    .map_err(|ce| corrupt(format!("recomputing batch failed: {ce}")))?;
                // 与存储 cells 比对（顺序无关）。
                let mut want = computed.cells;
                let mut got = e.cells;
                want.sort_by_key(|c| (c.ix, c.iy));
                got.sort_by_key(|c| (c.ix, c.iy));
                if want != got {
                    return Err(corrupt("stored cells disagree with recomputation".into()));
                }
                ts.snapshots.push(Snapshot {
                    info: VersionInfo {
                        version: e.version,
                        update_count: e.updates.len(),
                        created_at_ms: e.created_at_ms,
                        base_version: e.base_version,
                    },
                    values: computed.values,
                    bit: computed.bit,
                });
            }
        }
        Ok(())
    }
}
