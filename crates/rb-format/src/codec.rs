//! 二进制序列化格式 v1（魔数 `RBS1`）与严格校验。
//!
//! ## 布局（小端字节序）
//!
//! ```text
//! 偏移  长度  字段
//! 0     4     magic            = b"RBS1"
//! 4     4     version          = 0x00010000
//! 8     4     container_count  容器目录项数
//! 12    4     header_crc32c    对字节 [0,12) 的 CRC32C
//! 16    N     directory        container_count 个 16 字节目录项
//! 16+N  M     payloads         各容器负载（顺序与目录一致）
//! 末尾  4     body_crc32c      对 [16, 16+N+M) 的 CRC32C
//! ```
//!
//! ### 目录项（16 字节）
//!
//! | 偏移 | 长度 | 字段 |
//! |---|---|---|
//! | 0 | 2 | high key（高 16 位） |
//! | 2 | 1 | 类型标签（1=array / 2=bitmap） |
//! | 3 | 1 | 保留，必须为 0 |
//! | 4 | 4 | 负载长度（字节） |
//! | 8 | 4 | 负载偏移（相对体区起点，即绝对偏移 16） |
//! | 12 | 4 | 基数（元素个数） |
//!
//! ### 负载
//!
//! - array：`长度/2` 个 u16 小端，严格升序唯一，基数 = 长度/2 ≤ 4096；
//! - bitmap：恰好 8192 字节（1024 个 u64 小端），popcount = 基数，且基数 > 4096。
//!
//! 解码时逐项校验：魔数/版本、头部与体校验和、标签、键有序唯一、
//! 偏移与长度（类型约束、边界、顺序、无重叠）、数组有序唯一、阈值、
//! 声明基数与实际基数一致。任何不符都返回分类明确的 [`CodecError`]。

use std::io;

use crate::container::Container;
use crate::crc32c;
use crate::error::{CodecError, Result};
use crate::roaring::RoaringSet;
use crate::{
    ARRAY_MAX_CARDINALITY, BITMAP_WORDS, CONTAINER_BITS, FORMAT_VERSION, MAGIC, TAG_ARRAY,
    TAG_BITMAP,
};

const HEADER_LEN: usize = 16;
const ENTRY_LEN: usize = 16;

/// 序列化为 v1 二进制格式。
pub fn encode(set: &RoaringSet) -> Vec<u8> {
    let count = set.container_count();

    // 先生成体区负载（目录顺序 = 键升序）。
    let mut payloads: Vec<(u8, Vec<u8>, u32)> = Vec::with_capacity(count);
    for idx in 0..count {
        let c = set.container_at(idx);
        let (tag, bytes, card) = match c {
            Container::Array(v) => {
                let mut bytes = Vec::with_capacity(v.len() * 2);
                for x in v {
                    bytes.extend_from_slice(&x.to_le_bytes());
                }
                (TAG_ARRAY, bytes, v.len() as u32)
            }
            Container::Bitmap(b) => {
                let mut bytes = Vec::with_capacity(BITMAP_WORDS * 8);
                for word in b.iter() {
                    bytes.extend_from_slice(&word.to_le_bytes());
                }
                (TAG_BITMAP, bytes, c.len() as u32)
            }
        };
        payloads.push((tag, bytes, card));
    }

    let body_len: usize = payloads.iter().map(|(_, b, _)| b.len()).sum();
    let dir_len = count
        .checked_mul(ENTRY_LEN)
        .expect("container count overflow");
    let total = HEADER_LEN + dir_len + body_len + 4;
    let mut out = Vec::with_capacity(total);

    out.extend_from_slice(&MAGIC);
    out.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
    out.extend_from_slice(&(count as u32).to_le_bytes());
    let header_crc = crc32c::checksum(&out[..12]);
    out.extend_from_slice(&header_crc.to_le_bytes());

    // 目录与负载：目录必须在体区之前，因此先写目录占位偏移，负载连续排列。
    let mut cursor: u32 = dir_len as u32; // 相对体区起点（=目录之后）
    let dir_start = out.len();
    for (idx, (key, (tag, bytes, card))) in set.keys().iter().zip(payloads.iter()).enumerate() {
        let _ = idx;
        out.extend_from_slice(&key.to_le_bytes());
        out.push(*tag);
        out.push(0); // 保留字节
        out.extend_from_slice(&(bytes.len() as u32).to_le_bytes());
        out.extend_from_slice(&cursor.to_le_bytes());
        out.extend_from_slice(&card.to_le_bytes());
        cursor = cursor
            .checked_add(bytes.len() as u32)
            .expect("payload offset overflow");
    }
    debug_assert_eq!(out.len(), dir_start + dir_len);

    for (_, bytes, _) in &payloads {
        out.extend_from_slice(bytes);
    }

    let body_crc = crc32c::checksum(&out[HEADER_LEN..]);
    out.extend_from_slice(&body_crc.to_le_bytes());
    out
}

/// 从 v1 二进制严格校验解码。
pub fn decode(input: &[u8]) -> Result<RoaringSet> {
    if input.len() < HEADER_LEN + 4 {
        return Err(CodecError::Truncated {
            need: HEADER_LEN + 4,
            have: input.len(),
        });
    }

    let mut magic = [0u8; 4];
    magic.copy_from_slice(&input[0..4]);
    if magic != MAGIC {
        return Err(CodecError::BadMagic(magic));
    }

    let version = read_u32(input, 4);
    if version != FORMAT_VERSION {
        return Err(CodecError::UnsupportedVersion(version));
    }

    let count = read_u32(input, 8) as usize;
    let stored_header_crc = read_u32(input, 12);
    let computed_header_crc = crc32c::checksum(&input[..12]);
    if stored_header_crc != computed_header_crc {
        return Err(CodecError::HeaderChecksumMismatch {
            stored: stored_header_crc,
            computed: computed_header_crc,
        });
    }

    // 体区 = 目录 + 负载；末尾 4 字节是体 CRC。
    let dir_len = count.checked_mul(ENTRY_LEN).ok_or(CodecError::Truncated {
        need: usize::MAX,
        have: input.len(),
    })?;
    let body_with_crc_len = input
        .len()
        .checked_sub(HEADER_LEN)
        .ok_or(CodecError::Truncated {
            need: HEADER_LEN,
            have: input.len(),
        })?;
    let body_len = body_with_crc_len
        .checked_sub(4)
        .ok_or(CodecError::Truncated {
            need: HEADER_LEN + 4,
            have: input.len(),
        })?;
    if body_len < dir_len {
        return Err(CodecError::Truncated {
            need: HEADER_LEN + dir_len + 4,
            have: input.len(),
        });
    }

    // 结构长度校验：目录声明的负载必须完整落在体区内。
    // 先解析目录中的长度/偏移（此时 CRC 尚未验证，仅做不越界读取），
    // 确保任何截断都归类为 Truncated 而非误报 CRC 不匹配。
    let mut declared_need = dir_len;
    for i in 0..count {
        let base = HEADER_LEN + i * ENTRY_LEN;
        let payload_len = read_u32(input, base + 4) as usize;
        let offset = read_u32(input, base + 8) as usize;
        let end = offset
            .checked_add(payload_len)
            .ok_or(CodecError::Truncated {
                need: usize::MAX,
                have: input.len(),
            })?;
        // 负载末端超出实际体区 → 文件被截断（可能伴随 CRC 错，但截断更准确）。
        if end > body_len {
            return Err(CodecError::Truncated {
                need: HEADER_LEN + end + 4,
                have: input.len(),
            });
        }
        // 偏移指向目录区或体区之前 → 结构性非法偏移。
        if offset < dir_len {
            return Err(CodecError::BadOffset {
                key: read_u16(input, base),
                offset: offset as u32,
            });
        }
        if end > declared_need {
            declared_need = end;
        }
    }
    if declared_need > body_len {
        return Err(CodecError::Truncated {
            need: HEADER_LEN + declared_need + 4,
            have: input.len(),
        });
    }

    let stored_body_crc = read_u32(input, HEADER_LEN + body_len);
    let computed_body_crc = crc32c::checksum(&input[HEADER_LEN..HEADER_LEN + body_len]);
    if stored_body_crc != computed_body_crc {
        return Err(CodecError::BodyChecksumMismatch {
            stored: stored_body_crc,
            computed: computed_body_crc,
        });
    }

    // 解析目录（CRC 已通过，下面做结构/语义校验）。
    struct Entry {
        key: u16,
        tag: u8,
        payload_len: u32,
        offset: u32,
        cardinality: u32,
    }

    let mut entries = Vec::with_capacity(count);
    let mut prev_key: Option<u16> = None;
    for i in 0..count {
        let base = HEADER_LEN + i * ENTRY_LEN;
        let key = read_u16(input, base);
        let tag = input[base + 2];
        let reserved = input[base + 3];
        let payload_len = read_u32(input, base + 4);
        let offset = read_u32(input, base + 8);
        let cardinality = read_u32(input, base + 12);

        if reserved != 0 {
            return Err(CodecError::UnknownContainerTag(reserved));
        }
        if tag != TAG_ARRAY && tag != TAG_BITMAP {
            return Err(CodecError::UnknownContainerTag(tag));
        }
        if let Some(p) = prev_key {
            if key <= p {
                return Err(CodecError::KeysNotSortedUnique {
                    previous: Some(p),
                    current: key,
                });
            }
        }
        prev_key = Some(key);

        let allowed = match tag {
            TAG_ARRAY => {
                if payload_len % 2 != 0 {
                    return Err(CodecError::BadPayloadLength {
                        key,
                        declared: payload_len,
                        allowed: payload_len & !1,
                    });
                }
                payload_len
            }
            TAG_BITMAP => (BITMAP_WORDS * 8) as u32,
            _ => unreachable!(),
        };
        if payload_len != allowed {
            return Err(CodecError::BadPayloadLength {
                key,
                declared: payload_len,
                allowed,
            });
        }

        // 偏移/长度必须落在体区负载范围内（目录占体区前 dir_len 字节）。
        let start = offset as u64;
        let end = start + payload_len as u64;
        if (offset as usize) < dir_len || end > body_len as u64 || (offset as u64) > u32::MAX as u64
        {
            return Err(CodecError::BadOffset { key, offset });
        }

        entries.push(Entry {
            key,
            tag,
            payload_len,
            offset,
            cardinality,
        });
    }

    // 负载必须按目录顺序连续、无重叠地覆盖体区（偏移严格递增且首尾相接）。
    let mut expected = dir_len as u32;
    for e in &entries {
        if e.offset != expected {
            return Err(CodecError::BadOffset {
                key: e.key,
                offset: e.offset,
            });
        }
        expected = expected
            .checked_add(e.payload_len)
            .ok_or(CodecError::BadOffset {
                key: e.key,
                offset: e.offset,
            })?;
    }
    if expected as usize != body_len {
        return Err(CodecError::BadOffset {
            key: entries.last().map(|e| e.key).unwrap_or(0),
            offset: expected,
        });
    }

    // 逐容器解析并校验负载语义。
    let mut set = RoaringSet::new();
    for e in entries {
        let start = HEADER_LEN + e.offset as usize;
        let payload = &input[start..start + e.payload_len as usize];

        let container = if e.tag == TAG_ARRAY {
            let n = (e.payload_len / 2) as usize;
            let mut values = Vec::with_capacity(n);
            let mut prev: Option<u16> = None;
            for k in 0..n {
                let v = read_u16(payload, k * 2);
                if let Some(p) = prev {
                    if v <= p {
                        return Err(CodecError::ArrayNotSortedUnique {
                            key: e.key,
                            index: k,
                        });
                    }
                }
                prev = Some(v);
                values.push(v);
            }
            if n > ARRAY_MAX_CARDINALITY {
                return Err(CodecError::ArrayExceedsThreshold {
                    key: e.key,
                    cardinality: n,
                });
            }
            if e.cardinality != n as u32 {
                return Err(CodecError::CardinalityMismatch {
                    key: e.key,
                    declared: e.cardinality,
                    actual: n as u32,
                });
            }
            Container::from_sorted_unique(values)
        } else {
            let mut words = [0u64; BITMAP_WORDS];
            for (k, slot) in words.iter_mut().enumerate() {
                *slot = read_u64(payload, k * 8);
            }
            let actual = words.iter().map(|w| w.count_ones()).sum::<u32>();
            if actual != e.cardinality {
                return Err(CodecError::CardinalityMismatch {
                    key: e.key,
                    declared: e.cardinality,
                    actual,
                });
            }
            if actual <= ARRAY_MAX_CARDINALITY as u32 {
                // 位图容器必须保持稠密（基数 > 4096），否则表示非规范。
                return Err(CodecError::ArrayExceedsThreshold {
                    key: e.key,
                    cardinality: actual as usize,
                });
            }
            Container::Bitmap(Box::new(words))
        };

        // 容器覆盖 65536 值，基数不可能超过该值（防御性校验）。
        if container.len() as u32 > CONTAINER_BITS {
            return Err(CodecError::TrailingBitsSet { key: e.key });
        }
        set.insert_container(e.key, container);
    }

    Ok(set)
}

/// 编码到任意 [`io::Write`]。
pub fn write_to<W: io::Write>(set: &RoaringSet, w: &mut W) -> io::Result<()> {
    w.write_all(&encode(set))
}

/// 从任意 [`io::Read`] 读取全部字节后解码。
pub fn read_from<R: io::Read>(r: &mut R) -> Result<RoaringSet> {
    let mut buf = Vec::new();
    io::Read::read_to_end(r, &mut buf)?;
    decode(&buf)
}

#[inline]
fn read_u16(buf: &[u8], at: usize) -> u16 {
    u16::from_le_bytes([buf[at], buf[at + 1]])
}

#[inline]
fn read_u32(buf: &[u8], at: usize) -> u32 {
    u32::from_le_bytes([buf[at], buf[at + 1], buf[at + 2], buf[at + 3]])
}

#[inline]
fn read_u64(buf: &[u8], at: usize) -> u64 {
    u64::from_le_bytes([
        buf[at],
        buf[at + 1],
        buf[at + 2],
        buf[at + 3],
        buf[at + 4],
        buf[at + 5],
        buf[at + 6],
        buf[at + 7],
    ])
}
