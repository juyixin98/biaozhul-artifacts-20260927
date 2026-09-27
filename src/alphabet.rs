//! 字节文本与哨兵的唯一编码。
//!
//! 设计约束（需求：哨兵编码唯一且不能与正文冲突）：
//! - 正文是任意字节串，字节取值 `0..=255` 全部可能出现（含二进制零字节）。
//! - 编码后正文符号为 `byte + 1`，即取值 `1..=256`。
//! - 哨兵固定为 `SENTINEL = 0`，在数值上严格小于一切正文符号，
//!   因此不可能与任何正文字节冲突，也保证后缀数组中以哨兵开头的后缀排在第一行。
//! - 字母表大小固定 257（FM 索引天然支持全字节域，无需收集实际字符集）。

use crate::error::{FmError, Result};

/// 哨兵符号。
pub const SENTINEL: u16 = 0;
/// 字母表大小：0（哨兵）+ 1..=256（256 个字节）。
pub const ALPHABET_SIZE: usize = 257;

/// 单个正文字节 → 正文符号（`1..=256`）。
#[inline]
pub fn encode_byte(b: u8) -> u16 {
    b as u16 + 1
}

/// 正文符号 → 正文字节；哨兵返回 `None`。
#[inline]
pub fn decode_symbol(s: u16) -> Option<u8> {
    match s {
        SENTINEL => None,
        1..=256 => Some((s - 1) as u8),
        other => unreachable!("编码内核中不可能出现越界符号 {other}"),
    }
}

/// 字节文本 → 带唯一哨兵结尾的符号序列 `T + [0]`，长度为 `text.len() + 1`。
///
/// 空文本也允许编码（得到只含哨兵的长度 1 序列）；
/// 但建立索引时空文本属于 [`FmError::InvalidInput`]，在编码之前由上层拒绝。
pub fn encode_text(text: &[u8]) -> Vec<u16> {
    let mut syms = Vec::with_capacity(text.len() + 1);
    syms.extend(text.iter().map(|&b| encode_byte(b)));
    syms.push(SENTINEL);
    syms
}

/// 查询模式（字节）→ 符号序列。模式不含哨兵。
pub fn encode_pattern(pattern: &[u8]) -> Vec<u16> {
    pattern.iter().map(|&b| encode_byte(b)).collect()
}

/// 校验某符号数组恰好以一个哨兵结尾，且其余符号都在正文域内。
/// 从持久化数据重建内核时调用，防止坏文件污染后续计算。
pub fn validate_encoded(syms: &[u16]) -> Result<()> {
    let last = syms
        .last()
        .ok_or_else(|| FmError::corrupt("编码序列为空，缺少哨兵"))?;
    if *last != SENTINEL {
        return Err(FmError::corrupt("编码序列末尾不是哨兵 0"));
    }
    for (i, &s) in syms[..syms.len() - 1].iter().enumerate() {
        if s == SENTINEL {
            return Err(FmError::corrupt(format!(
                "哨兵在正文位置 {i} 提前出现，违反唯一编码"
            )));
        }
        if s > 256 {
            return Err(FmError::corrupt(format!("位置 {i} 出现越界符号 {s}")));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sentinel_is_less_than_every_text_symbol() {
        for b in 0u16..=255 {
            assert!(SENTINEL < b + 1);
        }
        assert_eq!(ALPHABET_SIZE, 257);
    }

    #[test]
    fn zero_byte_does_not_collide_with_sentinel() {
        // 二进制零字节是高频混淆点：正文 0x00 -> 符号 1，哨兵是符号 0。
        assert_eq!(encode_byte(0x00), 1);
        assert_eq!(decode_symbol(1), Some(0x00));
        assert_eq!(decode_symbol(SENTINEL), None);
    }

    #[test]
    fn encode_appends_single_sentinel() {
        let syms = encode_text(&[0, 255, 1]);
        assert_eq!(syms, vec![1, 256, 2, 0]);
        assert!(validate_encoded(&syms).is_ok());
    }

    #[test]
    fn validate_rejects_embedded_or_missing_sentinel() {
        assert!(validate_encoded(&[1, 0, 2, 0]).is_err());
        assert!(validate_encoded(&[1, 2]).is_err());
        assert!(validate_encoded(&[]).is_err());
    }
}
