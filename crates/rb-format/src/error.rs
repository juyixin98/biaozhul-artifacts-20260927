//! 编解码错误类型。失败被细分为确定的类别，调用方（测试、HTTP 接口）可以
//! 断言具体的失败原因，而不是只判断“出错了”。

use std::fmt;

/// rb-format 专用 `Result`。
pub type Result<T> = std::result::Result<T, CodecError>;

/// 反序列化 / 校验失败的具体类别。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CodecError {
    /// 文件不足 16 字节头部，或声明的长度超过实际数据。
    Truncated {
        /// 期望的最小字节位置。
        need: usize,
        /// 实际可用字节数。
        have: usize,
    },
    /// 魔数不匹配。
    BadMagic([u8; 4]),
    /// 版本不受支持。
    UnsupportedVersion(u32),
    /// 头部 CRC32C 与重算值不一致（文件头被篡改）。
    HeaderChecksumMismatch {
        /// 头部存储的校验值。
        stored: u32,
        /// 根据头部字节重算的值。
        computed: u32,
    },
    /// 体区 CRC32C 不一致（容器数据被篡改）。
    BodyChecksumMismatch { stored: u32, computed: u32 },
    /// 容器类型标签不是已知值（1=array / 2=bitmap）。
    UnknownContainerTag(u8),
    /// 目录项的高 16 位键未严格升序、或重复。
    KeysNotSortedUnique {
        /// 前一个键（若存在）。
        previous: Option<u16>,
        /// 违规的键。
        current: u16,
    },
    /// 负载偏移越界、与目录顺序不一致或互相重叠。
    BadOffset {
        /// 所属容器键。
        key: u16,
        /// 声明的负载偏移。
        offset: u32,
    },
    /// 声明的负载长度与容器类型不符。
    BadPayloadLength {
        key: u16,
        /// 声明的负载长度。
        declared: u32,
        /// 该容器类型允许的合法长度。
        allowed: u32,
    },
    /// 数组容器元素未严格升序、或重复（不满足有序唯一）。
    ArrayNotSortedUnique {
        key: u16,
        /// 第一个违规位置（元素下标）。
        index: usize,
    },
    /// 数组容器基数超过稀疏阈值（应以位图容器存储）。
    ArrayExceedsThreshold { key: u16, cardinality: usize },
    /// 目录声明基数与实际负载基数不一致。
    CardinalityMismatch {
        key: u16,
        declared: u32,
        actual: u32,
    },
    /// 位图容器在“最后一个置位之后”存在多余置位（冗余位），
    /// 即 popcount 超过声明基数。该变体同时覆盖低位冗余与整体不一致。
    TrailingBitsSet { key: u16 },
    /// 读写底层流失败。
    Io(String),
}

impl fmt::Display for CodecError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CodecError::Truncated { need, have } => write!(
                f,
                "truncated input: need at least {need} bytes, have {have}"
            ),
            CodecError::BadMagic(m) => write!(
                f,
                "bad magic: expected {:02X?}, found {:02X?}",
                crate::MAGIC,
                m
            ),
            CodecError::UnsupportedVersion(v) => {
                write!(f, "unsupported format version {:#010x}", v)
            }
            CodecError::HeaderChecksumMismatch { stored, computed } => write!(
                f,
                "header CRC32C mismatch: stored {stored:#010x}, computed {computed:#010x}"
            ),
            CodecError::BodyChecksumMismatch { stored, computed } => write!(
                f,
                "body CRC32C mismatch: stored {stored:#010x}, computed {computed:#010x}"
            ),
            CodecError::UnknownContainerTag(t) => {
                write!(f, "unknown container type tag {t}")
            }
            CodecError::KeysNotSortedUnique { previous, current } => write!(
                f,
                "container keys not strictly ascending/unique: {previous:?} followed by {current}"
            ),
            CodecError::BadOffset { key, offset } => {
                write!(f, "bad payload offset for key {key}: {offset}")
            }
            CodecError::BadPayloadLength {
                key,
                declared,
                allowed,
            } => write!(
                f,
                "bad payload length for key {key}: declared {declared}, allowed {allowed}"
            ),
            CodecError::ArrayNotSortedUnique { key, index } => write!(
                f,
                "array container key {key} element {index} violates sorted-unique order"
            ),
            CodecError::ArrayExceedsThreshold { key, cardinality } => write!(
                f,
                "array container key {key} cardinality {cardinality} exceeds threshold {}",
                crate::ARRAY_MAX_CARDINALITY
            ),
            CodecError::CardinalityMismatch {
                key,
                declared,
                actual,
            } => write!(
                f,
                "cardinality mismatch for key {key}: declared {declared}, actual {actual}"
            ),
            CodecError::TrailingBitsSet { key } => write!(
                f,
                "bitmap container key {key} has bits set beyond declared cardinality"
            ),
            CodecError::Io(m) => write!(f, "i/o error: {m}"),
        }
    }
}

impl From<std::io::Error> for CodecError {
    fn from(e: std::io::Error) -> Self {
        CodecError::Io(e.to_string())
    }
}

impl std::error::Error for CodecError {}
