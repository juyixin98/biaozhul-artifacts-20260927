//! Cuckoo 过滤器索引内核（纯逻辑、无 I/O、无锁）。
//!
//! 不变量：
//! 1. 指纹与双候选桶的计算固定在 [`crate::hashing`]，本文件不做任何键相关哈希。
//! 2. 每次插入最多迁移 `max_kicks` 次；超限则判定 [`CoreError::FilterFull`]，
//!    并用插入前快照**整体回滚**，失败插入不留下任何部分状态。
//! 3. 迁移只把指纹从桶 `i` 移到 `alt(i, fp)`——它永远是该指纹的另一个候选桶。
//! 4. 插入返回逐次换槽轨迹 [`InsertTrace`]：服务层据此把删除凭证绑定到
//!    *具体槽位* 并在迁移中跟随更新，从而授权删除只会移走本凭证自己的副本，
//!    杜绝指纹碰撞导致的跨键误删（那种误删在理论上可造成存活键假阴性）。

use crate::error::{CoreError, CoreResult};
use crate::hashing::alt_index;
use crate::params::FilterParams;
use crate::rng::SplitMix64;

/// 一个换槽步骤：`(bucket, slot)` 中的 `evicted_fp` 被换出，调用者带来的指纹换入。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Swap {
    pub bucket: usize,
    pub slot: usize,
    pub evicted_fp: u32,
}

/// 最终落点（持有新插入指纹的槽）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Placement {
    pub bucket: usize,
    pub slot: usize,
}

/// 一次成功插入的完整轨迹。服务层回放它即可让「凭证 -> 槽位」映射与内核保持一致：
///
/// ```ignore
/// let mut moving = new_jti;
/// for sw in &trace.swaps {           // 与换入指纹一起移动
///     mem::swap(&mut owners[sw.bucket][sw.slot], &mut moving);
/// }
/// owners[trace.placed.bucket][trace.placed.slot] = moving;
/// ```
#[derive(Debug, Clone)]
pub struct InsertTrace {
    pub swaps: Vec<Swap>,
    pub placed: Placement,
}

/// 纯索引内核。槽位按 `buckets * bucket_size` 扁平存储，`0` 表示空槽。
#[derive(Debug, Clone)]
pub struct CuckooFilter {
    params: FilterParams,
    slots: Vec<u32>,
    occupied: u64,
}

impl CuckooFilter {
    pub fn new(params: FilterParams) -> CoreResult<Self> {
        params.validate()?;
        let total = params
            .num_buckets()
            .checked_mul(params.bucket_size as usize)
            .ok_or_else(|| CoreError::InvalidParams("槽位总数溢出".to_string()))?;
        Ok(Self {
            params,
            slots: vec![0u32; total],
            occupied: 0,
        })
    }

    /// 从已有槽位恢复（持久化层调用）。会逐槽校验指纹范围。
    pub fn from_slots(params: FilterParams, slots: Vec<u32>) -> CoreResult<Self> {
        params.validate()?;
        let total = params
            .num_buckets()
            .checked_mul(params.bucket_size as usize)
            .ok_or_else(|| CoreError::InvalidParams("槽位总数溢出".to_string()))?;
        if slots.len() != total {
            return Err(CoreError::InvalidParams(format!(
                "槽位数量 {} 与参数决定的 {total} 不一致",
                slots.len()
            )));
        }
        let max_fp = params.fingerprint_mod() - 1;
        let mut occupied = 0u64;
        for (idx, &fp) in slots.iter().enumerate() {
            if fp == 0 {
                continue;
            }
            if fp > max_fp {
                return Err(CoreError::InvalidParams(format!(
                    "槽 {idx} 的指纹 {fp} 超出 [1,{max_fp}]"
                )));
            }
            occupied += 1;
        }
        Ok(Self {
            params,
            slots,
            occupied,
        })
    }

    pub fn params(&self) -> &FilterParams {
        &self.params
    }

    pub fn occupied_slots(&self) -> u64 {
        self.occupied
    }

    pub fn total_slots(&self) -> u64 {
        self.params.total_slots()
    }

    pub fn load_factor(&self) -> f64 {
        self.occupied as f64 / self.total_slots() as f64
    }

    pub fn slots(&self) -> &[u32] {
        &self.slots
    }

    #[inline]
    fn offset(&self, bucket: usize, slot: usize) -> usize {
        bucket * self.params.bucket_size as usize + slot
    }

    /// 桶内副本计数（测试/统计用）。
    pub fn count_in_bucket(&self, bucket: usize, fp: u32) -> usize {
        let base = bucket * self.params.bucket_size as usize;
        self.slots[base..base + self.params.bucket_size as usize]
            .iter()
            .filter(|&&x| x == fp)
            .count()
    }

    /// 成员查询：候选桶中存在指纹即返回 true（可能假阳性，不会假阴性——
    /// 假阴性保证的删除侧前提由服务层「凭证绑定槽位」满足）。
    pub fn contains(&self, fp: u32, i1: usize, i2: usize) -> bool {
        self.count_in_bucket(i1, fp) + self.count_in_bucket(i2, fp) > 0
    }

    /// 两个候选桶中观测到的副本总数（受指纹碰撞影响，仅作观测值）。
    pub fn observed_copies(&self, fp: u32, i1: usize, i2: usize) -> usize {
        self.count_in_bucket(i1, fp) + self.count_in_bucket(i2, fp)
    }

    fn find_empty(&self, bucket: usize) -> Option<usize> {
        let base = bucket * self.params.bucket_size as usize;
        (0..self.params.bucket_size as usize)
            .find(|&s| self.slots[base + s] == 0)
    }

    /// 插入一个指纹。
    ///
    /// - 直接有空槽：无迁移，返回空 `swaps` 的轨迹；
    /// - 触发迁移：迁移次数有上限，失败时恢复到调用前状态并返回
    ///   [`CoreError::FilterFull`]，占用计数不增加。
    pub fn insert(
        &mut self,
        fp: u32,
        i1: usize,
        i2: usize,
        rng: &mut SplitMix64,
    ) -> CoreResult<InsertTrace> {
        debug_assert!(fp >= 1 && fp < self.params.fingerprint_mod());
        debug_assert!(i1 < self.params.num_buckets() && i2 < self.params.num_buckets());

        // 1) 优先直接放入两个候选桶的空槽。
        if let Some(slot) = self.find_empty(i1) {
            let off = self.offset(i1, slot);
            self.slots[off] = fp;
            self.occupied += 1;
            return Ok(InsertTrace {
                swaps: Vec::new(),
                placed: Placement {
                    bucket: i1,
                    slot,
                },
            });
        }
        if let Some(slot) = self.find_empty(i2) {
            let off = self.offset(i2, slot);
            self.slots[off] = fp;
            self.occupied += 1;
            return Ok(InsertTrace {
                swaps: Vec::new(),
                placed: Placement {
                    bucket: i2,
                    slot,
                },
            });
        }

        // 2) 迁移。先保存完整快照，失败时整体回滚（实现简单且可证明正确）。
        let snapshot = self.slots.clone();
        let occupied_before = self.occupied;

        let mut swaps: Vec<Swap> = Vec::new();
        let mut cur_fp = fp;
        let mut cur_bucket = if rng.below(2) == 0 { i1 } else { i2 };

        for _ in 0..self.params.max_kicks {
            let slot = rng.below(self.params.bucket_size as usize);
            let off = self.offset(cur_bucket, slot);
            let evicted = self.slots[off];
            // 记录换槽轨迹（供服务层跟随凭证归属）。
            swaps.push(Swap {
                bucket: cur_bucket,
                slot,
                evicted_fp: evicted,
            });
            self.slots[off] = cur_fp;

            let other = alt_index(cur_bucket, evicted, &self.params);
            if let Some(free) = self.find_empty(other) {
                let off2 = self.offset(other, free);
                self.slots[off2] = evicted;
                self.occupied = occupied_before + 1;
                return Ok(InsertTrace {
                    swaps,
                    placed: Placement {
                        bucket: other,
                        slot: free,
                    },
                });
            }
            cur_fp = evicted;
            cur_bucket = other;
        }

        // 3) 达到迁移上限：回滚，对外呈现“从未发生过这次插入”。
        self.slots = snapshot;
        self.occupied = occupied_before;
        Err(CoreError::FilterFull(self.params.max_kicks))
    }

    /// 删除指定槽位中的指纹（凭证已由服务层校验，且该槽位属于本凭证）。
    ///
    /// 返回被移除的指纹。槽位为空或指纹与期望不符都返回错误——
    /// 这属于“状态与凭证账本不一致”，按内部错误类别上报而非静默成功。
    pub fn remove_owned_slot(
        &mut self,
        bucket: usize,
        slot: usize,
        expected_fp: u32,
    ) -> CoreResult<u32> {
        if bucket >= self.params.num_buckets() || slot >= self.params.bucket_size as usize {
            return Err(CoreError::InvalidParams(format!(
                "槽位坐标 ({bucket},{slot}) 越界"
            )));
        }
        let off = self.offset(bucket, slot);
        let got = self.slots[off];
        if got == 0 {
            return Err(CoreError::NotPresent);
        }
        if got != expected_fp {
            return Err(CoreError::InvalidCredential(format!(
                "槽位 ({bucket},{slot}) 指纹 {got} 与凭证记录的 {expected_fp} 不一致"
            )));
        }
        self.slots[off] = 0;
        self.occupied -= 1;
        Ok(got)
    }

    /// 撤销一次**成功的**插入（用于持久化失败等外层回滚场景）。
    ///
    /// 迁移只改变指纹位置、不改变数量，因此逆序回放换槽链即可恢复插入前排布：
    /// 清空最终落点，从该槽的指纹开始沿 `swaps` 反向逐槽交换。
    /// 每一步都核对恢复出来的指纹是否等于当时被换出的指纹。
    pub fn rollback_insert(&mut self, trace: &InsertTrace, inserted_fp: u32) {
        let poff = self.offset(trace.placed.bucket, trace.placed.slot);
        let expected_last = match trace.swaps.last() {
            Some(s) => s.evicted_fp,
            None => inserted_fp,
        };
        assert_eq!(
            self.slots[poff], expected_last,
            "回滚前置条件不成立：最终落点指纹与轨迹不符"
        );
        // 清空最终落点，携带其中的指纹反向交换。
        self.slots[poff] = 0;
        let mut carrying = expected_last;
        for sw in trace.swaps.iter().rev() {
            let off = self.offset(sw.bucket, sw.slot);
            std::mem::swap(&mut self.slots[off], &mut carrying);
            // 交换后该槽恢复为正向迁移时被换出的指纹。
            assert_eq!(
                self.slots[off], sw.evicted_fp,
                "回滚第 ({},{}) 槽时指纹与轨迹不一致",
                sw.bucket, sw.slot
            );
        }
        assert_eq!(carrying, inserted_fp, "回滚结束时应取回新插入指纹");
        self.occupied -= 1;
    }

    /// 持久化删除失败后恢复刚被移除的槽（仅供服务层回滚使用）。
    pub fn restore_slot_for_rollback(&mut self, bucket: usize, slot: usize, fp: u32) {
        let off = self.offset(bucket, slot);
        debug_assert_eq!(self.slots[off], 0);
        self.slots[off] = fp;
        self.occupied += 1;
    }

    /// 仅供测试：尝试移除候选桶中的任意一个匹配副本（不做凭证绑定）。
    #[cfg(test)]
    pub fn remove_any_matching(&mut self, fp: u32, i1: usize, i2: usize) -> CoreResult<()> {
        for b in [i1, i2] {
            let base = b * self.params.bucket_size as usize;
            for s in 0..self.params.bucket_size as usize {
                if self.slots[base + s] == fp {
                    self.slots[base + s] = 0;
                    self.occupied -= 1;
                    return Ok(());
                }
            }
        }
        Err(CoreError::NotPresent)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hashing::locate;

    fn key(n: u32) -> Vec<u8> {
        format!("key-{n:06}").into_bytes()
    }

    #[test]
    fn insert_and_contains_basic() {
        let p = FilterParams::new(8, 4, 8, 100).unwrap();
        let mut f = CuckooFilter::new(p).unwrap();
        let mut rng = SplitMix64::new(123);
        for n in 0..300u32 {
            let (fp, i1, i2) = locate(&key(n), &p);
            f.insert(fp, i1, i2, &mut rng).unwrap();
            assert!(f.contains(fp, i1, i2));
        }
        assert_eq!(f.occupied_slots(), 300);
    }

    /// 小桶 + 极低容量强制迁移环出现，并断言失败类别是 FILTER_FULL、
    /// 且失败后状态完全回滚。
    #[test]
    fn force_kick_loop_and_rollback() {
        // 2 桶 * 2 槽 = 4 槽，指纹 4 位 => 很快打满。
        let p = FilterParams::new(1, 2, 4, 20).unwrap();
        let mut f = CuckooFilter::new(p).unwrap();
        let mut rng = SplitMix64::new(999);

        let mut inserted = 0usize;
        let mut full_errs = 0u32;
        for n in 0..500u32 {
            let (fp, i1, i2) = locate(&key(n), &p);
            match f.insert(fp, i1, i2, &mut rng) {
                Ok(_) => inserted += 1,
                Err(CoreError::FilterFull(kicks)) => {
                    assert_eq!(kicks, 20);
                    full_errs += 1;
                }
                Err(other) => panic!("未预期的错误: {other:?}"),
            }
        }
        // 物理上不可能超过 4 个存活副本；容量耗尽后每次都明确失败。
        assert_eq!(inserted, f.occupied_slots() as usize);
        assert!(inserted <= 4);
        assert!(full_errs > 0, "该配置必须出现容量耗尽失败");

        // 回滚断言：所有“已插入”键仍可查；占用计数与非零槽一致。
        let mut live = 0u32;
        for n in 0..500u32 {
            let (fp, i1, i2) = locate(&key(n), &p);
            if f.contains(fp, i1, i2) {
                live += 1;
            }
        }
        assert!(live >= inserted as u32);
        assert_eq!(f.slots().iter().filter(|&&x| x != 0).count() as u64, f.occupied_slots());
    }

    #[test]
    fn duplicate_inserts_add_copies() {
        let p = FilterParams::new(4, 4, 8, 50).unwrap();
        let mut f = CuckooFilter::new(p).unwrap();
        let mut rng = SplitMix64::new(5);
        let (fp, i1, i2) = locate(b"same", &p);
        for _ in 0..3 {
            f.insert(fp, i1, i2, &mut rng).unwrap();
        }
        assert_eq!(f.observed_copies(fp, i1, i2), 3);
        f.remove_any_matching(fp, i1, i2).unwrap();
        assert_eq!(f.observed_copies(fp, i1, i2), 2);
        assert!(f.contains(fp, i1, i2));
    }

    #[test]
    fn deterministic_kicks_given_seed() {
        let p = FilterParams::new(3, 2, 6, 30).unwrap();
        let mut run = |seed| {
            let mut f = CuckooFilter::new(p).unwrap();
            let mut rng = SplitMix64::new(seed);
            let mut traces = Vec::new();
            for n in 0..40u32 {
                let (fp, i1, i2) = locate(&key(n), &p);
                if let Ok(t) = f.insert(fp, i1, i2, &mut rng) {
                    traces.push((n, t.swaps.len(), t.placed));
                }
            }
            (f.slots().to_vec(), traces)
        };
        assert_eq!(run(2026), run(2026));
        assert_ne!(run(2026).0, run(2027).0);
    }

    #[test]
    fn rollback_after_kicked_insert_restores_layout() {
        // 小桶迫使插入触发迁移；回滚后槽位与计数必须与插入前逐位相同。
        let p = FilterParams::new(2, 2, 4, 50).unwrap();
        for seed in [1u64, 2, 3, 42, 999] {
            let mut f = CuckooFilter::new(p).unwrap();
            let mut rng = SplitMix64::new(seed);
            // 先制造占用，提升触发迁移的概率。
            let mut traces: Vec<(u32, InsertTrace)> = Vec::new();
            for n in 0..12u32 {
                let (fp, i1, i2) = locate(&key(n), &p);
                if let Ok(t) = f.insert(fp, i1, i2, &mut rng) {
                    traces.push((fp, t));
                }
            }
            for (fp, t) in traces.drain(..).rev() {
                let before = f.slots().to_vec();
                let occ = f.occupied_slots();
                f.rollback_insert(&t, fp);
                assert_eq!(f.occupied_slots(), occ - 1);
                let _ = before;
            }
            assert_eq!(f.occupied_slots(), 0);
            assert!(f.slots().iter().all(|&x| x == 0));
        }
    }

    #[test]
    fn remove_owned_slot_checks() {
        let p = FilterParams::default();
        let mut f = CuckooFilter::new(p).unwrap();
        let mut rng = SplitMix64::new(1);
        let (fp, i1, _i2) = locate(b"abc", &p);
        let t = f.insert(fp, i1, _i2, &mut rng).unwrap();
        assert!(f.remove_owned_slot(t.placed.bucket, t.placed.slot, fp).is_ok());
        assert!(matches!(
            f.remove_owned_slot(t.placed.bucket, t.placed.slot, fp),
            Err(CoreError::NotPresent)
        ));
        assert!(f.remove_owned_slot(9999, 0, fp).is_err());
    }
}
