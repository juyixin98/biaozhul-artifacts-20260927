//! Occ/rank 结构：支持 `rank_c(i) = BWT[0..i] 中符号 c 的出现次数`（i 为半开边界）。
//!
//! 空间/时间权衡采用“分块全检查点”：
//! - 每隔 `block` 个 BWT 位置存一张完整 257 项计数表；
//! - 查询时取最近检查点，再线性补齐至多 `block-1` 个位置，O(block)。
//!
//! block 默认 256：单次 rank 最多扫 255 个 u16，内存开销约
//! `(n/block) × 257 × 4` 字节（n=1M 时约 4 MB）。
//!
//! 同时提供该结构的紧凑二进制编解码（小端），供持久化层落盘与损坏检测。

use crate::alphabet::ALPHABET_SIZE;
use crate::error::{FmError, Result};

/// rank 检查点结构，拥有 BWT 最后一列。
#[derive(Debug, Clone)]
pub struct Occ {
    /// 检查点块大小（>= 1）。
    block: u64,
    bwt: Vec<u16>,
    /// `checks[j]` = BWT[0 .. j*block] 的逐符号计数；长度 `ceil(n/block)+1`。
    checks: Vec<[u32; ALPHABET_SIZE]>,
}

impl Occ {
    /// 由 BWT 列构造检查点。
    pub fn new(bwt: Vec<u16>, block: u64) -> Result<Self> {
        if block == 0 {
            return Err(FmError::invalid_input("rank 检查点块大小不能为 0"));
        }
        let n = bwt.len() as u64;
        let nblocks = n.div_ceil(block) as usize;
        let mut checks: Vec<[u32; ALPHABET_SIZE]> = vec![[0u32; ALPHABET_SIZE]; nblocks + 1];
        for (bi, chunk) in bwt.chunks(block as usize).enumerate() {
            let mut acc = checks[bi];
            for &s in chunk {
                acc[s as usize] += 1;
            }
            checks[bi + 1] = acc;
        }
        Ok(Occ { block, bwt, checks })
    }

    pub fn block_size(&self) -> u64 {
        self.block
    }

    pub fn bwt(&self) -> &[u16] {
        &self.bwt
    }

    pub fn len(&self) -> u64 {
        self.bwt.len() as u64
    }

    /// BWT 不会为空（至少含哨兵）；方法仅为满足 clippy 的 len/is_empty 约定。
    pub fn is_empty(&self) -> bool {
        self.bwt.is_empty()
    }

    /// `BWT[0..i]` 中符号 `symbol` 的出现次数。要求 `0 <= i <= n`。
    #[inline]
    pub fn rank(&self, symbol: u16, i: u64) -> u64 {
        assert!(
            i <= self.len(),
            "rank 查询边界 {i} 超出 BWT 长度 {}",
            self.len()
        );
        let block = self.block;
        let j = (i / block) as usize;
        let start = j as u64 * block;
        let mut count = self.checks[j][symbol as usize] as u64;
        for &s in &self.bwt[start as usize..i as usize] {
            if s == symbol {
                count += 1;
            }
        }
        count
    }

    // ---- 紧凑二进制编码（小端）----
    //
    // 布局：
    //   magic "OCC1"(4) | block u64 | n u64 | bwt: n×u16 | checks: (nblocks+1)×257×u32

    pub fn to_bytes(&self) -> Vec<u8> {
        let n = self.bwt.len() as u64;
        let mut out =
            Vec::with_capacity(16 + self.bwt.len() * 2 + self.checks.len() * ALPHABET_SIZE * 4);
        out.extend_from_slice(b"OCC1");
        out.extend_from_slice(&self.block.to_le_bytes());
        out.extend_from_slice(&n.to_le_bytes());
        for &s in &self.bwt {
            out.extend_from_slice(&s.to_le_bytes());
        }
        for table in &self.checks {
            for v in table.iter() {
                out.extend_from_slice(&v.to_le_bytes());
            }
        }
        out
    }

    /// 解析并做完整的自洽校验：任何截断/越界/不一致都报 [`FmError::Corrupt`]。
    pub fn from_bytes(bytes: &[u8]) -> Result<Self> {
        // 顺序读取切片的游标。
        struct Cursor<'a> {
            buf: &'a [u8],
        }
        impl<'a> Cursor<'a> {
            fn take(&mut self, k: usize, what: &str) -> Result<&'a [u8]> {
                if self.buf.len() < k {
                    return Err(FmError::corrupt(format!("occ 文件截断：缺少 {what}")));
                }
                let (head, rest) = self.buf.split_at(k);
                self.buf = rest;
                Ok(head)
            }
        }

        let mut cur = Cursor { buf: bytes };
        let magic = cur.take(4, "magic")?;
        if magic != b"OCC1" {
            return Err(FmError::corrupt("occ 文件 magic 不是 OCC1"));
        }
        let block = u64::from_le_bytes(cur.take(8, "block")?.try_into().unwrap());
        if block == 0 {
            return Err(FmError::corrupt("occ 文件中 block 为 0"));
        }
        let n = u64::from_le_bytes(cur.take(8, "n")?.try_into().unwrap());

        let bwt_bytes = cur.take(
            (n as usize)
                .checked_mul(2)
                .ok_or_else(|| FmError::corrupt("occ 文件长度字段溢出"))?,
            "bwt 数据",
        )?;
        let mut bwt = Vec::with_capacity(n as usize);
        let (pairs, rem) = bwt_bytes.as_chunks::<2>();
        if !rem.is_empty() {
            return Err(FmError::corrupt("bwt 数据不是 2 字节对齐"));
        }
        for pair in pairs {
            let s = u16::from_le_bytes(*pair);
            if s as usize >= ALPHABET_SIZE {
                return Err(FmError::corrupt(format!("BWT 符号 {s} 越界")));
            }
            bwt.push(s);
        }

        let nblocks = n.div_ceil(block) as usize;
        let table_bytes = cur.take(
            (nblocks + 1)
                .checked_mul(ALPHABET_SIZE)
                .and_then(|v| v.checked_mul(4))
                .ok_or_else(|| FmError::corrupt("occ 检查点大小溢出"))?,
            "检查点数据",
        )?;
        let mut checks: Vec<[u32; ALPHABET_SIZE]> = Vec::with_capacity(nblocks + 1);
        let (tables, trem) = table_bytes.as_chunks::<{ ALPHABET_SIZE * 4 }>();
        if !trem.is_empty() {
            return Err(FmError::corrupt("检查点数据不是整张表对齐"));
        }
        for table in tables {
            let mut arr = [0u32; ALPHABET_SIZE];
            let (quads, qrem) = table.as_chunks::<4>();
            if !qrem.is_empty() {
                return Err(FmError::corrupt("检查点表内 u32 不对齐"));
            }
            for (i, quad) in quads.iter().enumerate() {
                arr[i] = u32::from_le_bytes(*quad);
            }
            checks.push(arr);
        }
        if !cur.buf.is_empty() {
            return Err(FmError::corrupt("occ 文件尾部有多余字节"));
        }

        // 自洽性校验：重建全部检查点并与落盘值逐块比对。
        let occ = Occ { block, bwt, checks };
        occ.validate_checks()?;
        Ok(occ)
    }

    /// 重建全部检查点并与落盘值逐块比对。
    fn validate_checks(&self) -> Result<()> {
        let n = self.bwt.len() as u64;
        if self.checks.len() != n.div_ceil(self.block) as usize + 1 {
            return Err(FmError::corrupt("occ 检查点表数量与 block/n 不符"));
        }
        if self.checks[0].iter().any(|&v| v != 0) {
            return Err(FmError::corrupt("occ 首个检查点应为全零"));
        }
        let mut rebuilt = [0u32; ALPHABET_SIZE];
        for (bi, chunk) in self.bwt.chunks(self.block as usize).enumerate() {
            for &s in chunk {
                rebuilt[s as usize] += 1;
            }
            if rebuilt != self.checks[bi + 1] {
                return Err(FmError::corrupt(format!(
                    "occ 第 {bi} 块后检查点与 BWT 重算结果不一致"
                )));
            }
        }
        let total: u64 = self.checks[self.checks.len() - 1]
            .iter()
            .map(|&v| v as u64)
            .sum();
        if total != n {
            return Err(FmError::corrupt("occ 末检查点计数合计不等于 BWT 长度"));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 朴素前缀计数，独立于检查点实现。
    fn naive_rank(bwt: &[u16], symbol: u16, i: usize) -> u64 {
        bwt[..i].iter().filter(|&&s| s == symbol).count() as u64
    }

    #[test]
    fn rank_matches_prefix_counts_banana() {
        // banana$ 的 BWT：b n n a $ a a
        let a = b'a' as u16 + 1;
        let bwt: Vec<u16> = vec![
            b'b' as u16 + 1,
            b'n' as u16 + 1,
            b'n' as u16 + 1,
            a,
            0,
            a,
            a,
        ];
        for block in [1u64, 2, 3, 7, 100] {
            let occ = Occ::new(bwt.clone(), block).unwrap();
            for i in 0..=bwt.len() {
                for s in 0u16..=256 {
                    assert_eq!(
                        occ.rank(s, i as u64),
                        naive_rank(&bwt, s, i),
                        "block={block} i={i} symbol={s}"
                    );
                }
            }
        }
    }

    #[test]
    fn codec_roundtrip() {
        let bwt: Vec<u16> = vec![1, 0, 256, 1, 3, 0, 256];
        let occ = Occ::new(bwt.clone(), 2).unwrap();
        let bytes = occ.to_bytes();
        let back = Occ::from_bytes(&bytes).unwrap();
        assert_eq!(back.bwt(), occ.bwt());
        assert_eq!(back.block_size(), 2);
        for i in 0..=bwt.len() as u64 {
            assert_eq!(back.rank(256, i), occ.rank(256, i));
        }
    }

    #[test]
    fn codec_rejects_truncation_and_bad_values() {
        let bwt: Vec<u16> = vec![1, 2, 3, 0];
        let good = Occ::new(bwt, 2).unwrap().to_bytes();
        // 各种截断都必须报 corrupt
        for cut in [0usize, 4, 10, good.len() - 1] {
            assert!(matches!(
                Occ::from_bytes(&good[..cut]),
                Err(FmError::Corrupt { .. })
            ));
        }
        // 坏 magic
        let mut bad = good.clone();
        bad[0] = b'X';
        assert!(matches!(
            Occ::from_bytes(&bad),
            Err(FmError::Corrupt { .. })
        ));
        // 尾部多余字节
        let mut tail = good.clone();
        tail.push(0);
        assert!(matches!(
            Occ::from_bytes(&tail),
            Err(FmError::Corrupt { .. })
        ));
    }
}
