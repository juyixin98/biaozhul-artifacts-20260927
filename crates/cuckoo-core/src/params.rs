//! 过滤器参数与不变量校验。
//!
//! 所有影响存储格式与索引结果的参数都集中在这里；参数一旦写入磁盘快照即被固定，
//! 后续进程必须以完全相同的参数打开（见 `cuckoo_persist` 的头部校验）。

use crate::error::CoreError;

/// 每个桶的槽位数（fingerprint 个数）。
///
/// 取 4 是 Cuckoo Filter 文献（Fan et al., 2014）给出的标准配置；
/// 小桶（`bucket_size = 2`）也受支持，独立测试用它强制提前出现迁移环。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FilterParams {
    /// 桶数量，必须是 2 的幂；`b * bucket_size` 为物理槽位总数。
    pub buckets_exp: u32,
    /// 每个桶的槽位数（2 或 4）。
    pub bucket_size: u32,
    /// 指纹位宽 f，满足 4 <= f <= 16。
    pub fingerprint_bits: u32,
    /// 插入时最大迁移（kick）次数；超过即判定本次插入失败并整体回滚。
    pub max_kicks: u32,
}

impl Default for FilterParams {
    fn default() -> Self {
        // 默认 4096 桶 * 4 槽 = 16384 槽。
        Self {
            buckets_exp: 12,
            bucket_size: 4,
            fingerprint_bits: 12,
            max_kicks: 500,
        }
    }
}

impl FilterParams {
    pub const MIN_FP_BITS: u32 = 4;
    pub const MAX_FP_BITS: u32 = 16;

    pub fn new(
        buckets_exp: u32,
        bucket_size: u32,
        fingerprint_bits: u32,
        max_kicks: u32,
    ) -> Result<Self, CoreError> {
        let p = Self {
            buckets_exp,
            bucket_size,
            fingerprint_bits,
            max_kicks,
        };
        p.validate()?;
        Ok(p)
    }

    pub fn validate(&self) -> Result<(), CoreError> {
        if !(1..=31).contains(&self.buckets_exp) {
            return Err(CoreError::InvalidParams(format!(
                "buckets_exp={} 超出范围 [1,31]（桶数必须是 2 的幂）",
                self.buckets_exp
            )));
        }
        if !matches!(self.bucket_size, 2 | 4) {
            return Err(CoreError::InvalidParams(format!(
                "bucket_size={} 仅支持 2 或 4",
                self.bucket_size
            )));
        }
        if !(Self::MIN_FP_BITS..=Self::MAX_FP_BITS).contains(&self.fingerprint_bits) {
            return Err(CoreError::InvalidParams(format!(
                "fingerprint_bits={} 超出范围 [{},{}]",
                self.fingerprint_bits,
                Self::MIN_FP_BITS,
                Self::MAX_FP_BITS
            )));
        }
        if self.max_kicks == 0 {
            return Err(CoreError::InvalidParams(
                "max_kicks 必须大于 0".to_string(),
            ));
        }
        Ok(())
    }

    /// 桶数量（2 的幂）。
    pub fn num_buckets(&self) -> usize {
        1usize << self.buckets_exp
    }

    /// 物理槽位总数。
    pub fn total_slots(&self) -> u64 {
        self.num_buckets() as u64 * self.bucket_size as u64
    }

    /// 指纹掩码 +1，例如 12 位 => 0x1000。
    pub fn fingerprint_mod(&self) -> u32 {
        1u32 << self.fingerprint_bits
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_and_rejects_params() {
        assert!(FilterParams::default().validate().is_ok());
        assert!(FilterParams::new(0, 4, 12, 10).is_err());
        assert!(FilterParams::new(10, 3, 12, 10).is_err());
        assert!(FilterParams::new(10, 4, 3, 10).is_err());
        assert!(FilterParams::new(10, 4, 17, 10).is_err());
        assert!(FilterParams::new(10, 4, 12, 0).is_err());
        let p = FilterParams::new(3, 2, 8, 7).unwrap();
        assert_eq!(p.num_buckets(), 8);
        assert_eq!(p.total_slots(), 16);
        assert_eq!(p.fingerprint_mod(), 256);
    }
}
