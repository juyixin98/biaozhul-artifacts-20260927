//! 验证器的独立参考模型：按 Cuckoo Filter 规范重新实现插入/迁移，
//! 用于推导「给定固定 seed 与固定输入序列时的期望最终布局」。
//!
//! 独立性说明：
//! - 哈希（指纹/候选桶）来自验证器自己的 [`crate::xxh64`]，不用被测 crate；
//! - 本文件的迁移循环是对算法规范的独立抄写，不引用 `cuckoo_core::filter`；
//! - 若被测实现与模型在同一 seed、同一输入序列下产生不同布局，
//!   要么实现偏离规范、要么模型错误——二者都要求人工复核，不能放过。

use crate::oracle::{expected_locate, ExpectedLocation};

/// 验证器自有 SplitMix64（与文档伪码对照抄写，非 import 被测代码）。
pub struct RefSplitMix64 {
    state: u64,
}

impl RefSplitMix64 {
    pub fn new(seed: u64) -> Self {
        Self { state: seed }
    }
    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    pub fn below(&mut self, n: usize) -> usize {
        (self.next_u64() % n as u64) as usize
    }
}

/// 参考模型的插入结果。
#[derive(Debug, PartialEq, Eq)]
pub enum RefOutcome {
    Placed {
        placed_bucket: usize,
        placed_slot: usize,
        /// 每次迁移换出的指纹（与被测内核 Swap.evicted_fp 对照）。
        evicted_chain: Vec<u32>,
    },
    Full {
        kicks_attempted: u32,
    },
}

pub struct RefFilter {
    slots: Vec<u32>,
    buckets_exp: u32,
    bucket_size: usize,
    max_kicks: u32,
}

impl RefFilter {
    pub fn new(buckets_exp: u32, bucket_size: u32, max_kicks: u32) -> Self {
        let n = (1usize << buckets_exp) * bucket_size as usize;
        Self {
            slots: vec![0; n],
            buckets_exp,
            bucket_size: bucket_size as usize,
            max_kicks,
        }
    }

    pub fn slots(&self) -> &[u32] {
        &self.slots
    }

    fn alt(&self, idx: usize, fp: u32) -> usize {
        // 复用 oracle 的固定常量路径（独立实现哈希）。
        let key = fp.to_le_bytes();
        // expected_locate 不直接给 alt；这里手工实现同规范。
        let h = crate::xxh64::xxh64(&key, crate::oracle::SEED_FP_INDEX);
        let nb = 1usize << self.buckets_exp;
        (idx ^ (h % nb as u64) as usize) & (nb - 1)
    }

    fn empty_slot(&self, bucket: usize) -> Option<usize> {
        let base = bucket * self.bucket_size;
        (0..self.bucket_size).find(|&s| self.slots[base + s] == 0)
    }

    /// 插入 loc 描述的键。与被测内核遵循同一份迁移规范：
    /// 先试两个候选桶空槽；否则随机选一个候选桶起迁，随机踢槽，最多 max_kicks 次。
    pub fn insert(&mut self, loc: &ExpectedLocation, rng: &mut RefSplitMix64) -> RefOutcome {
        let fp = loc.fp;
        let i1 = loc.i1;
        let i2 = loc.i2;

        if let Some(s) = self.empty_slot(i1) {
            self.slots[i1 * self.bucket_size + s] = fp;
            return RefOutcome::Placed {
                placed_bucket: i1,
                placed_slot: s,
                evicted_chain: vec![],
            };
        }
        if let Some(s) = self.empty_slot(i2) {
            self.slots[i2 * self.bucket_size + s] = fp;
            return RefOutcome::Placed {
                placed_bucket: i2,
                placed_slot: s,
                evicted_chain: vec![],
            };
        }

        // 迁移前保存快照；达到上限时恢复到插入前状态（与被测内核的回滚语义一致）。
        let snapshot = self.slots.clone();
        let mut cur_fp = fp;
        let mut cur_bucket = if rng.below(2) == 0 { i1 } else { i2 };
        let mut chain = Vec::new();

        for _ in 0..self.max_kicks {
            let slot = rng.below(self.bucket_size);
            let off = cur_bucket * self.bucket_size + slot;
            let evicted = self.slots[off];
            chain.push(evicted);
            self.slots[off] = cur_fp;
            let other = self.alt(cur_bucket, evicted);
            if let Some(free) = self.empty_slot(other) {
                self.slots[other * self.bucket_size + free] = evicted;
                return RefOutcome::Placed {
                    placed_bucket: other,
                    placed_slot: free,
                    evicted_chain: chain,
                };
            }
            cur_fp = evicted;
            cur_bucket = other;
        }
        self.slots = snapshot;
        RefOutcome::Full {
            kicks_attempted: self.max_kicks,
        }
    }

    /// 失败插入后模型保持的状态需由调用方恢复（参考模型直接重建整个过滤器更简单，
    /// 场景中用“逐次记录成功序列、失败即停止”的方式比较，回滚正确性另做专门测试）。
    pub fn loc_for(&self, key: &[u8], fp_bits: u32) -> ExpectedLocation {
        expected_locate(key, self.buckets_exp, fp_bits)
    }
}
