//! # rb-format
//!
//! 分层位图集合（layered / hybrid bitmap set）的数据格式与运算内核。
//!
//! 结构参考 Roaring Bitmap 的分层思想，但本实现完全独立：
//!
//! - 32 位整数空间按高 16 位划分为最多 65536 个 **容器（container）**；
//! - 每个容器覆盖连续的 65536 个值（低 16 位）；
//! - 基数不超过 [`ARRAY_MAX_CARDINALITY`]（=4096）时使用排序去重的稀疏数组
//!   [`Container::Array`]；超过阈值时使用 8 KiB 稠密位图 [`Container::Bitmap`]；
//! - 容器的选择由固定阈值 [`ARRAY_MAX_CARDINALITY`] 唯一决定，集合运算后
//!   重新规范化（canonicalize），保证同一逻辑集合的表示唯一。
//!
//! 运算（并/交/差、rank/select）直接在容器内部结构上进行，**从不**把集合
//! 展开为完整整数列表再做集合运算（详见 [`ops`] 模块文档）。

#![forbid(unsafe_code)]

pub mod codec;
pub mod container;
pub mod crc32c;
pub mod error;
pub mod ops;
pub mod roaring;

pub use container::Container;
pub use error::{CodecError, Result};
pub use roaring::RoaringSet;

/// 测试/校验辅助：在不经过公开插入路径的情况下构造分片，
/// 以及检查容器的物理表示。需要 `test-util` 特性开启。
#[cfg(feature = "test-util")]
pub mod test_util {
    use super::container::{clear_bit, set_bit};
    use super::{Container, RoaringSet, BITMAP_WORDS};

    /// 用显式高 16 位键和已构造容器组装集合（会规范化、丢弃空容器）。
    pub fn insert_raw(set: &mut RoaringSet, high_key: u16, container: Container) {
        set.insert_container(high_key, container);
    }

    /// 从一组低分片值构造容器（经阈值规范化）。
    pub fn container_from_lows(lows: Vec<u16>) -> Container {
        Container::from_values(lows)
    }

    /// 构造一个位图容器并直接指定位（随后规范化）。
    pub fn bitmap_with_bits(lows: impl IntoIterator<Item = u16>) -> Container {
        let mut words = Box::new([0u64; BITMAP_WORDS]);
        for l in lows {
            set_bit(&mut words, l);
        }
        let mut c = Container::Bitmap(words);
        c.canonicalize();
        c
    }

    /// 在已有的位图字数组上置位（可变访问）。
    pub fn set(words: &mut [u64; BITMAP_WORDS], low: u16) {
        set_bit(words, low);
    }

    /// 清除一个位。
    pub fn clear(words: &mut [u64; BITMAP_WORDS], low: u16) {
        clear_bit(words, low);
    }

    /// 直接用位图字构造容器（不额外规范化，用于制造非规范损坏样本）。
    pub fn raw_bitmap(words: [u64; BITMAP_WORDS]) -> Container {
        Container::Bitmap(Box::new(words))
    }

    pub fn is_array(c: &Container) -> bool {
        c.is_array()
    }
}

/// 低 16 位空间位数：每个容器覆盖 2^16 = 65536 个连续整数。
pub const CONTAINER_BITS: u32 = 1 << 16;

/// 稠密位图容器使用的 u64 字数：65536 / 64 = 1024。
pub const BITMAP_WORDS: usize = CONTAINER_BITS as usize / u64::BITS as usize;

/// 稀疏数组 ⇄ 稠密位图切换的固定基数阈值。
///
/// 基数 `<= 4096` 规范为数组容器，`> 4096` 规范为位图容器。
/// （两者在 4096 处字节体积相同：2*4096 = 8*1024。）
pub const ARRAY_MAX_CARDINALITY: usize = 4096;

/// 当前二进制格式的语义版本（major*65536 + minor）。
pub const FORMAT_VERSION: u32 = 0x0001_0000;

/// 文件魔数：ASCII "RBS1"（Roaring-Bitmap-Set v1）。
pub const MAGIC: [u8; 4] = *b"RBS1";

/// 集合可容纳的最大整数值（u32::MAX）。所有 rank/计数路径使用 u64，
/// 因此“最大整数边界不溢出”。
pub const MAX_VALUE: u32 = u32::MAX;

/// 容器类型标签：稀疏数组。
pub const TAG_ARRAY: u8 = 1;
/// 容器类型标签：稠密位图。
pub const TAG_BITMAP: u8 = 2;
