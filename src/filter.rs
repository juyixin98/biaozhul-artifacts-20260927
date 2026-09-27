//! Cuckoo 过滤器桶表：插入、有上限的迁移（kick）、失败整体回滚、查询与删除。
//!
//! # 为什么槽里不只有指纹
//!
//! 教科书式 Cuckoo 过滤器在插入时只要候选桶里**已存在相同指纹**就幂等去重。这在
//! 「两个不同的键恰好像像（指纹相同且候选桶相交）」时会让它们共用一个槽：删除其中
//! 一个键就会移除共享指纹，使另一个仍然存活的键出现**假阴性**。本实现的硬性要求是
//! 「只允许假阳性、不允许正常数据假阴性」，因此每个槽存：
//!
//! ```text
//! Cell { fingerprint: u32, copies: u32, owner: [u8;32] }
//! ```
//!
//! * 不同键（`owner` 不同）即使指纹相同也各自占用独立槽位，互不影响；
//! * 同一键重复插入只在其槽位上 `copies += 1`，不重复占槽（计数语义）；
//! * 删除必须携带与插入令牌一致的 `owner`（键标识），只递减该拥有者的副本数；
//!   其他键的槽位在任何情况下都不会被触碰。
//!
//! 由此，只要记账层显示某键仍有存活副本，它的指纹槽就必然还在其两个候选桶之一，
//! 成员判定不可能为假阴性。成员判定仍只比对指纹，因此**假阳性**性质与标准 Cuckoo
//! 过滤器一致（约 `2b/2^f`）。
//!
//! # 迁移与回滚
//!
//! 迁移以整个 `Cell` 为单位移动。失败时沿迁移链反向交换，字节级恢复调用前状态
//! （`rollback_is_byte_exact` 黄金测试逐槽断言）；迁移次数受 `max_kicks` 限制。

use rand::{Rng, SeedableRng};
use rand_chacha::ChaCha20Rng;
use serde::{Deserialize, Serialize};

use crate::hashing::{alt_index, place, KernelParams};

/// 过滤器配置（= 内核参数，迁移上限在内核中）。
pub use crate::hashing::KernelParams as FilterConfig;

/// 键拥有者标识（见 [`crate::token::key_id`]）。
pub type Owner = [u8; 32];

/// 插入尝试的分类结果（失败必须是具体类别，禁止统一报成功）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InsertOutcome {
    /// 新占用一个槽（可能经过 0..=max_kicks 次迁移），copies 置 1。
    Inserted { kicks: u32 },
    /// 同一 owner 已在候选桶：其 copies += 1，不新增槽位。
    Duplicate,
    /// 迁移达上限仍无空槽：已回滚，表恢复到调用前状态。
    FilterFull { attempted_kicks: u32 },
    /// 单键副本数达到 u32 上限（实际不可达，仍显式分类）。
    DuplicateLimit,
}

/// 一个桶槽。fingerprint==0 表示空槽（copies 必须为 0、owner 必须全零）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Cell {
    pub fingerprint: u32,
    pub copies: u32,
    pub owner: Owner,
}

impl Cell {
    pub const EMPTY: Cell = Cell {
        fingerprint: 0,
        copies: 0,
        owner: [0u8; 32],
    };

    pub fn is_empty(&self) -> bool {
        self.fingerprint == 0
    }
}

/// 一次删除移除/递减的位置信息（用于持久化失败时精确撤销）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Deletion {
    pub bucket: u64,
    pub slot: u32,
    pub fingerprint: u32,
    pub owner: Owner,
    /// 删除前的副本数；为 1 表示删除清空了槽位。
    pub copies_before: u32,
}

/// 一条迁移记录：坐标与迁移前该槽的完整单元。
#[derive(Debug, Clone, Copy)]
struct KickRecord {
    bucket: u64,
    slot: u32,
    before: Cell,
}

/// Cuckoo 桶表。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CuckooFilter {
    params: KernelParams,
    cells: Vec<Cell>,
    /// 已占用槽数（非空单元数）。
    occupied_cells: u64,
    /// 所有单元 copies 之和（= 记账层总存活副本数）。
    total_copies: u64,
}

impl CuckooFilter {
    /// 按参数创建空表。
    pub fn new(params: KernelParams) -> Self {
        let total = params
            .num_buckets
            .checked_mul(params.bucket_size as u64)
            .expect("桶表大小溢出");
        Self {
            params,
            cells: vec![Cell::EMPTY; total as usize],
            occupied_cells: 0,
            total_copies: 0,
        }
    }

    pub fn params(&self) -> &KernelParams {
        &self.params
    }

    pub fn num_buckets(&self) -> u64 {
        self.params.num_buckets
    }

    pub fn bucket_size(&self) -> u32 {
        self.params.bucket_size
    }

    pub fn fingerprint_bits(&self) -> u32 {
        self.params.fingerprint_bits
    }

    pub fn occupied_slots(&self) -> u64 {
        self.occupied_cells
    }

    /// 全部存活副本数（含重复键的多份拷贝）。
    pub fn total_copies(&self) -> u64 {
        self.total_copies
    }

    pub fn total_slots(&self) -> u64 {
        self.cells.len() as u64
    }

    /// 直接暴露单元表（持久化与逐槽测试使用）。
    pub fn cells(&self) -> &[Cell] {
        &self.cells
    }

    /// 从已解码单元重建（持久化层加载后校验用）。
    pub fn from_cells(
        params: KernelParams,
        cells: Vec<Cell>,
        total_copies: u64,
    ) -> Result<Self, FilterError> {
        let total = (params.num_buckets as usize)
            .checked_mul(params.bucket_size as usize)
            .ok_or(FilterError::Invalid("桶表大小溢出".into()))?;
        if cells.len() != total {
            return Err(FilterError::Invalid(format!(
                "槽数 {} 与参数期望 {} 不一致",
                cells.len(),
                total
            )));
        }
        let max_fp = max_fingerprint(params.fingerprint_bits);
        let mut occupied = 0u64;
        let mut copies_sum: u64 = 0;
        for c in &cells {
            match c.fingerprint {
                0 => {
                    if c.copies != 0 || c.owner != [0u8; 32] {
                        return Err(FilterError::Invalid("空槽携带 copies/owner".into()));
                    }
                }
                fp => {
                    if fp > max_fp {
                        return Err(FilterError::Invalid(format!("槽内指纹 {fp} 超出指纹空间")));
                    }
                    if c.copies == 0 {
                        return Err(FilterError::Invalid("非空槽 copies 为 0".into()));
                    }
                    occupied += 1;
                    copies_sum += c.copies as u64;
                }
            }
        }
        if copies_sum != total_copies {
            return Err(FilterError::Invalid(format!(
                "副本总数 {total_copies} 与单元求和 {copies_sum} 不一致"
            )));
        }
        Ok(Self {
            params,
            cells,
            occupied_cells: occupied,
            total_copies,
        })
    }

    #[inline]
    fn bucket_range(&self, b: u64) -> std::ops::Range<usize> {
        let start = b as usize * self.params.bucket_size as usize;
        start..start + self.params.bucket_size as usize
    }

    /// 成员查询：两个候选桶中是否存在匹配指纹（只读，可能假阳性）。
    pub fn contains(&self, key: &[u8]) -> bool {
        let p = place(key, &self.params);
        self.has_fingerprint(p.i1, p.fingerprint) || self.has_fingerprint(p.i2, p.fingerprint)
    }

    fn has_fingerprint(&self, bucket: u64, fp: u32) -> bool {
        self.cells[self.bucket_range(bucket)]
            .iter()
            .any(|c| c.fingerprint == fp)
    }

    fn find_owner(&self, bucket: u64, fp: u32, owner: Owner) -> Option<u32> {
        self.cells[self.bucket_range(bucket)]
            .iter()
            .position(|c| c.fingerprint == fp && c.owner == owner)
            .map(|s| s as u32)
    }

    fn first_empty(&self, bucket: u64) -> Option<u32> {
        self.cells[self.bucket_range(bucket)]
            .iter()
            .position(Cell::is_empty)
            .map(|s| s as u32)
    }

    /// 插入键（owner 为该键的键标识）。
    pub fn insert(&mut self, key: &[u8], owner: Owner) -> InsertOutcome {
        self.insert_with_rng(key, owner, &mut ChaCha20Rng::from_entropy())
    }

    /// 注入随机源的插入（测试用确定性种子复现迁移环）。
    pub fn insert_seeded(&mut self, key: &[u8], owner: Owner, rng_seed: u64) -> InsertOutcome {
        let mut rng = ChaCha20Rng::seed_from_u64(rng_seed);
        self.insert_with_rng(key, owner, &mut rng)
    }

    fn insert_with_rng<R: Rng>(&mut self, key: &[u8], owner: Owner, rng: &mut R) -> InsertOutcome {
        let p = place(key, &self.params);

        // 同一 owner 的重复插入：精确定位自己的槽，copies += 1（绝不去重到别的键）。
        for b in [p.i1, p.i2] {
            if let Some(s) = self.find_owner(b, p.fingerprint, owner) {
                let cell =
                    &mut self.cells[b as usize * self.params.bucket_size as usize + s as usize];
                if cell.copies == u32::MAX {
                    return InsertOutcome::DuplicateLimit;
                }
                cell.copies += 1;
                self.total_copies += 1;
                return InsertOutcome::Duplicate;
            }
        }

        // 新单元：先尝试两个候选桶的空槽（即使同指纹的其他 owner 已占槽也不复用）。
        let fresh = Cell {
            fingerprint: p.fingerprint,
            copies: 1,
            owner,
        };
        for b in [p.i1, p.i2] {
            if let Some(s) = self.first_empty(b) {
                self.place_into_empty(b, s, fresh);
                return InsertOutcome::Inserted { kicks: 0 };
            }
        }

        // 迁移：随机选一个候选桶为起点，整个 Cell 一起被踢出。
        // 整链只消耗一个空槽、只新增一份副本；被迁移的存量单元计数保持不变，因此
        // 全程用直接赋值，仅在最终落入空槽时统一 +1（occupied 与 total_copies）。
        let mut bucket = if rng.gen::<bool>() { p.i1 } else { p.i2 };
        let mut moving = fresh;
        let mut trace: Vec<KickRecord> = Vec::with_capacity(self.params.max_kicks as usize + 1);

        for kick in 0..self.params.max_kicks {
            let slot = rng.gen_range(0..self.params.bucket_size);
            let idx = bucket as usize * self.params.bucket_size as usize + slot as usize;
            let victim = self.cells[idx];
            debug_assert!(!victim.is_empty(), "满桶中被选中的槽必为非空");
            trace.push(KickRecord {
                bucket,
                slot,
                before: victim,
            });
            self.cells[idx] = moving;
            moving = victim;
            bucket = alt_index(
                bucket,
                moving.fingerprint,
                &self.params.seed,
                self.params.num_buckets,
                self.params.fingerprint_bits,
            );

            if let Some(s) = self.first_empty(bucket) {
                self.place_into_empty(bucket, s, moving);
                return InsertOutcome::Inserted { kicks: kick + 1 };
            }
        }

        // 达上限：沿迁移链反向交换，字节级回滚。
        self.rollback(&trace, moving);
        debug_assert!(
            self.find_owner(p.i1, p.fingerprint, owner).is_none()
                && self.find_owner(p.i2, p.fingerprint, owner).is_none(),
            "回滚后新 owner 不得残留"
        );
        InsertOutcome::FilterFull {
            attempted_kicks: self.params.max_kicks,
        }
    }

    /// 把单元放入已确认的空槽，并登记「整次插入净增一个占用槽、一份副本」。
    /// 迁移路径中被搬移的存量单元一律用直接赋值，绝不走这里，否则会重复计数。
    fn place_into_empty(&mut self, bucket: u64, slot: u32, cell: Cell) {
        let idx = bucket as usize * self.params.bucket_size as usize + slot as usize;
        debug_assert!(self.cells[idx].is_empty());
        self.cells[idx] = cell;
        self.occupied_cells += 1;
        self.total_copies += 1;
    }

    /// 反向撤销迁移：每步交换槽位与游离单元，逐步恢复迁移前局面。
    fn rollback(&mut self, trace: &[KickRecord], mut moving: Cell) {
        for rec in trace.iter().rev() {
            let idx = rec.bucket as usize * self.params.bucket_size as usize + rec.slot as usize;
            std::mem::swap(&mut self.cells[idx], &mut moving);
            debug_assert_eq!(self.cells[idx], rec.before, "回滚未恢复迁移前槽位");
        }
        // 计数只在 put_cell 真正落入空槽时变化；失败路径从未成功 put，无需调整。
    }

    /// 持凭证递减一个副本：按 (指纹, owner) 精确匹配，绝不触碰其他键的槽。
    pub fn decrement(
        &mut self,
        owner: Owner,
        i1: u64,
        i2: u64,
        fp: u32,
    ) -> Result<Deletion, FilterError> {
        let loc = self
            .find_owner(i1, fp, owner)
            .map(|s| (i1, s))
            .or_else(|| self.find_owner(i2, fp, owner).map(|s| (i2, s)));
        let (bucket, slot) = loc.ok_or_else(|| {
            FilterError::Invariant(
                "凭证已记账但桶表中找不到该 owner 的指纹单元（计数/表不一致或文件损坏）".into(),
            )
        })?;
        let idx = bucket as usize * self.params.bucket_size as usize + slot as usize;
        let copies_before = self.cells[idx].copies;
        debug_assert!(copies_before >= 1);
        let deletion = Deletion {
            bucket,
            slot,
            fingerprint: fp,
            owner,
            copies_before,
        };

        self.total_copies -= 1;
        if copies_before == 1 {
            self.cells[idx] = Cell::EMPTY;
            self.occupied_cells -= 1;
        } else {
            self.cells[idx].copies -= 1;
        }
        Ok(deletion)
    }

    /// 撤销一次删除（持久化失败的事务回滚）。
    pub fn restore_deletion(&mut self, d: &Deletion) {
        let idx = d.bucket as usize * self.params.bucket_size as usize + d.slot as usize;
        self.total_copies += 1;
        if d.copies_before == 1 {
            debug_assert!(self.cells[idx].is_empty(), "撤销清空式删除时目标槽应为空");
            self.cells[idx] = Cell {
                fingerprint: d.fingerprint,
                copies: 1,
                owner: d.owner,
            };
            self.occupied_cells += 1;
        } else {
            debug_assert_eq!(self.cells[idx].fingerprint, d.fingerprint);
            debug_assert_eq!(self.cells[idx].owner, d.owner);
            debug_assert_eq!(self.cells[idx].copies + 1, d.copies_before);
            self.cells[idx].copies += 1;
        }
    }

    /// 编码单元表：每单元 40 字节 = u32le(fp) || u32le(copies) || owner(32)。
    pub fn encode_cells(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(self.cells.len() * CELL_BYTES);
        for c in &self.cells {
            out.extend_from_slice(&c.fingerprint.to_le_bytes());
            out.extend_from_slice(&c.copies.to_le_bytes());
            out.extend_from_slice(&c.owner);
        }
        out
    }

    /// 解码单元表（持久化层用）。
    pub fn decode_cells(bytes: &[u8]) -> Result<Vec<Cell>, FilterError> {
        if bytes.len() % CELL_BYTES != 0 {
            return Err(FilterError::Invalid(format!(
                "槽区字节数 {} 不是 {CELL_BYTES} 的整数倍",
                bytes.len()
            )));
        }
        let mut cells = Vec::with_capacity(bytes.len() / CELL_BYTES);
        for chunk in bytes.chunks_exact(CELL_BYTES) {
            let fp = u32::from_le_bytes(chunk[0..4].try_into().unwrap());
            let copies = u32::from_le_bytes(chunk[4..8].try_into().unwrap());
            let mut owner = [0u8; 32];
            owner.copy_from_slice(&chunk[8..40]);
            cells.push(Cell {
                fingerprint: fp,
                copies,
                owner,
            });
        }
        Ok(cells)
    }

    /// 整表比较（测试用）。
    pub fn table_equal(&self, other: &Self) -> bool {
        self.cells == other.cells
            && self.occupied_cells == other.occupied_cells
            && self.total_copies == other.total_copies
    }
}

/// 单元在线/持久化编码的固定字节数。
pub const CELL_BYTES: usize = 40;

/// 过滤器层错误。
#[derive(Debug)]
pub enum FilterError {
    /// 参数/解码数据非法。
    Invalid(String),
    /// 内部不变式被破坏（计数与表不一致等）。
    Invariant(String),
}

impl std::fmt::Display for FilterError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FilterError::Invalid(s) | FilterError::Invariant(s) => f.write_str(s),
        }
    }
}
impl std::error::Error for FilterError {}

/// 指定位宽下最大合法指纹 = 2^f - 1。
pub fn max_fingerprint(bits: u32) -> u32 {
    if bits >= 32 {
        u32::MAX
    } else {
        (1u32 << bits) - 1
    }
}
