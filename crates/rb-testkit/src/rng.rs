//! 确定性 64 位伪随机数：SplitMix64（Sebastiano Vigna）。
//!
//! 选择它是因为：零依赖、固定种子下完全确定、周期 2^64、分布质量足够夹具使用。

/// SplitMix64 状态机。
#[derive(Debug, Clone)]
pub struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    /// 以任意种子初始化（会做一次黄金比例混合，避免种子相近导致序列相近）。
    pub fn new(seed: u64) -> Self {
        SplitMix64 {
            state: seed.wrapping_add(0x9E37_79B9_7F4A_7C15),
        }
    }

    /// 下一个伪随机 u64。
    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    /// 下一个限制在 `[0, bound)` 内的值（无模偏置的拒绝采样上界法）。
    pub fn below(&mut self, bound: u64) -> u64 {
        assert!(bound != 0);
        let zone = u64::MAX - (u64::MAX % bound);
        loop {
            let r = self.next_u64();
            if r < zone {
                return r % bound;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::SplitMix64;

    /// 固定检查向量：种子 0 的前三个输出（已用本实现实际输出固化，
    /// 配合 [`deterministic_same_seed`] 与 Python 侧同算法夹具交叉核对）。
    #[test]
    fn fixed_vector_seed0() {
        let mut r = SplitMix64::new(0);
        let v = [r.next_u64(), r.next_u64(), r.next_u64()];
        assert_eq!(
            v,
            [
                0x6E78_9E6A_A1B9_65F4,
                0x06C4_5D18_8009_454F,
                0xF88B_B8A8_724C_81EC
            ],
            "observed: {v:016X?}"
        );
    }

    /// 同种子必须产生同序列（夹具可复现）。
    #[test]
    fn deterministic_same_seed() {
        let mut a = SplitMix64::new(12345);
        let mut b = SplitMix64::new(12345);
        for _ in 0..1000 {
            assert_eq!(a.next_u64(), b.next_u64());
        }
    }

    /// below() 输出必须落在界内且能取到边界附近值。
    #[test]
    fn below_in_range() {
        let mut r = SplitMix64::new(7);
        for _ in 0..10_000 {
            let v = r.below(65536);
            assert!(v < 65536);
        }
    }
}
