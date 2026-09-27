//! 确定性的迁移随机源。
//!
//! 使用 SplitMix64（Sebastiano Vigna）。固定 `seed` 时，迁移路径完全可复现——
//! 独立测试据此断言固定的迁移序列与失败类别；生产路径由服务层传入随机种子。

#[derive(Debug, Clone)]
pub struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
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

    /// 返回 `[0, bound)` 内的值；`bound` 必须非零。
    pub fn below(&mut self, bound: usize) -> usize {
        debug_assert!(bound > 0);
        (self.next_u64() % bound as u64) as usize
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deterministic_sequence() {
        let mut r0 = SplitMix64::new(42);
        let a: Vec<u64> = (0..8).map(|_| r0.next_u64()).collect();
        let mut r = SplitMix64::new(42);
        let b: Vec<u64> = (0..8).map(|_| r.next_u64()).collect();
        assert_eq!(a, b);
        // 与另一固定种子不同（极小概率冲突，种子 1 vs 42 不会）。
        let first_other = SplitMix64::new(1).next_u64();
        assert_ne!(a[0], first_other);
    }

    #[test]
    fn below_in_bounds() {
        let mut r = SplitMix64::new(7);
        for _ in 0..1000 {
            assert!(r.below(4) < 4);
        }
        assert_eq!(r.below(1), 0);
    }
}
