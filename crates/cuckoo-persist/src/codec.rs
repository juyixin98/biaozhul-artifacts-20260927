//! 磁盘数据格式（格式版本 1，全部小端）。
//!
//! ```text
//! 偏移  长度  字段
//! 0     8    magic          b"CKCFILTR"
//! 8     4    format_version u32 (= 1)
//! 12    4    buckets_exp    u32
//! 16    4    bucket_size    u32
//! 20    4    fp_bits        u32 (4..=16)
//! 24    4    max_kicks      u32
//! 28    8    occupied       u64
//! 36    4    slots_len      u32 (= 2^buckets_exp * bucket_size)
//! 40    4    owners_off     u32 (= HEADER_LEN + 2*slots_len)
//! 44    4    reserved       u32 (=0)
//! 48    N    slots          每槽 u16（f <= 16），0 = 空槽
//! 48+N  16L  owners         与槽一一对应的 16 字节 jti；空槽为全 0
//! ...    8    checksum       XXH64(seed=CHECK_SEED) 覆盖此前所有字节
//! ```
//! 其中 `L = slots_len`。归属段和指纹槽位严格对齐：
//! 非空槽必须带非零 jti，空槽必须全零。装载时逐项核对并重建凭证账本。

use cuckoo_core::xxhash64;

pub const MAGIC: [u8; 8] = *b"CKCFILTR";
pub const FORMAT_VERSION: u32 = 1;
pub const CHECK_SEED: u64 = 0x434B_465F_4348_4B31; // "CKF_CHK1"

/// 头部固定长度。
pub const HEADER_LEN: usize = 48;
/// 每个归属槽的字节数（jti 为 128 位）。
pub const OWNER_LEN: usize = 16;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Snapshot {
    pub buckets_exp: u32,
    pub bucket_size: u32,
    pub fp_bits: u32,
    pub max_kicks: u32,
    pub occupied: u64,
    /// 长度恒等：`slots.len() == owners.len()`。
    pub slots: Vec<u16>,
    /// 每个槽对应一个 16 字节 jti（空槽为全零）。
    pub owners: Vec<[u8; OWNER_LEN]>,
}

impl Snapshot {
    pub fn encode(&self) -> Vec<u8> {
        assert_eq!(self.slots.len(), self.owners.len(), "slots/owners 长度不一致");
        let slots_len = self.slots.len() as u32;
        let owners_off = (HEADER_LEN + self.slots.len() * 2) as u32;
        let mut out = Vec::with_capacity(
            HEADER_LEN + self.slots.len() * 2 + self.owners.len() * OWNER_LEN + 8,
        );
        out.extend_from_slice(&MAGIC);
        out.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
        out.extend_from_slice(&self.buckets_exp.to_le_bytes());
        out.extend_from_slice(&self.bucket_size.to_le_bytes());
        out.extend_from_slice(&self.fp_bits.to_le_bytes());
        out.extend_from_slice(&self.max_kicks.to_le_bytes());
        out.extend_from_slice(&self.occupied.to_le_bytes());
        out.extend_from_slice(&slots_len.to_le_bytes());
        out.extend_from_slice(&owners_off.to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes()); // reserved
        for &s in &self.slots {
            out.extend_from_slice(&s.to_le_bytes());
        }
        for o in &self.owners {
            out.extend_from_slice(o);
        }
        let check = xxhash64(&out, CHECK_SEED);
        out.extend_from_slice(&check.to_le_bytes());
        out
    }

    /// 解码并做完整的结构、校验和与一致性检查。
    pub fn decode(buf: &[u8]) -> Result<Snapshot, CodecError> {
        if buf.len() < HEADER_LEN + 8 {
            return Err(CodecError::Truncated(buf.len()));
        }
        if buf[0..8] != MAGIC {
            return Err(CodecError::BadMagic);
        }
        let rd = Reader::new(buf);
        let version = rd.u32_at(8);
        if version != FORMAT_VERSION {
            return Err(CodecError::UnsupportedVersion { version });
        }
        let buckets_exp = rd.u32_at(12);
        let bucket_size = rd.u32_at(16);
        let fp_bits = rd.u32_at(20);
        let max_kicks = rd.u32_at(24);
        let occupied = rd.u64_at(28);
        let slots_len = rd.u32_at(36) as usize;
        let owners_off = rd.u32_at(40) as usize;

        // 参数范围检查（构造 FilterParams 时还会再严格校验一次）。
        if !(1..=31).contains(&buckets_exp) {
            return Err(CodecError::FieldOutOfRange("buckets_exp"));
        }
        if !matches!(bucket_size, 2 | 4) {
            return Err(CodecError::FieldOutOfRange("bucket_size"));
        }
        if !(4..=16).contains(&fp_bits) {
            return Err(CodecError::FieldOutOfRange("fp_bits"));
        }
        if max_kicks == 0 {
            return Err(CodecError::FieldOutOfRange("max_kicks"));
        }
        let expected_len = (1usize << buckets_exp)
            .checked_mul(bucket_size as usize)
            .ok_or(CodecError::LengthOverflow)?;
        if slots_len != expected_len {
            return Err(CodecError::SlotsLenMismatch {
                got: slots_len,
                expected: expected_len,
            });
        }

        let slots_end = HEADER_LEN
            .checked_add(slots_len.checked_mul(2).ok_or(CodecError::LengthOverflow)?)
            .ok_or(CodecError::LengthOverflow)?;
        if owners_off != slots_end {
            return Err(CodecError::OwnersOffsetMismatch {
                got: owners_off,
                expected: slots_end,
            });
        }
        let body_end = slots_end
            .checked_add(slots_len.checked_mul(OWNER_LEN).ok_or(CodecError::LengthOverflow)?)
            .ok_or(CodecError::LengthOverflow)?;
        if body_end.checked_add(8) != Some(buf.len()) {
            return Err(CodecError::LengthMismatch {
                got: buf.len(),
                expected: body_end + 8,
            });
        }

        let stored = rd.u64_at(body_end);
        let actual = xxhash64(&buf[..body_end], CHECK_SEED);
        if stored != actual {
            return Err(CodecError::ChecksumMismatch { stored, actual });
        }

        let max_fp = (1u32 << fp_bits) - 1;
        let mut slots = Vec::with_capacity(slots_len);
        let mut owners: Vec<[u8; OWNER_LEN]> = Vec::with_capacity(slots_len);
        let mut nonempty = 0u64;

        let mut off = HEADER_LEN;
        for _ in 0..slots_len {
            let fp = u16::from_le_bytes([buf[off], buf[off + 1]]);
            off += 2;
            let fp32 = fp as u32;
            if fp != 0 && fp32 > max_fp {
                return Err(CodecError::FingerprintOutOfRange(fp32));
            }
            if fp != 0 {
                nonempty += 1;
            }
            slots.push(fp);
        }

        let zero_jti = [0u8; OWNER_LEN];
        let mut o = owners_off;
        for &fp in &slots {
            let mut jti = [0u8; OWNER_LEN];
            jti.copy_from_slice(&buf[o..o + OWNER_LEN]);
            o += OWNER_LEN;
            match (fp, jti == zero_jti) {
                (0, false) => return Err(CodecError::OwnerWithoutSlot),
                (0, true) => {}
                (_, true) => return Err(CodecError::SlotWithoutOwner),
                _ => {}
            }
            owners.push(jti);
        }
        if occupied != nonempty {
            return Err(CodecError::OccupiedMismatch {
                header: occupied,
                counted: nonempty,
            });
        }

        // 归属 jti 不允许重复（每个凭证唯一对应一个槽）。
        let mut seen = std::collections::HashSet::new();
        for jti in owners.iter().filter(|j| **j != zero_jti) {
            if !seen.insert(*jti) {
                return Err(CodecError::DuplicateOwner);
            }
        }

        Ok(Snapshot {
            buckets_exp,
            bucket_size,
            fp_bits,
            max_kicks,
            occupied,
            slots,
            owners,
        })
    }
}

struct Reader<'a> {
    buf: &'a [u8],
}

impl<'a> Reader<'a> {
    fn new(buf: &'a [u8]) -> Self {
        Self { buf }
    }
    fn u32_at(&self, off: usize) -> u32 {
        u32::from_le_bytes(self.buf[off..off + 4].try_into().unwrap())
    }
    fn u64_at(&self, off: usize) -> u64 {
        u64::from_le_bytes(self.buf[off..off + 8].try_into().unwrap())
    }
}

#[derive(Debug, thiserror::Error)]
pub enum CodecError {
    #[error("数据被截断：只有 {0} 字节")]
    Truncated(usize),
    #[error("魔数不匹配，不是本服务的快照文件")]
    BadMagic,
    #[error("不支持的格式版本 {version}（本构建支持 v{FMT}）", FMT = FORMAT_VERSION)]
    UnsupportedVersion { version: u32 },
    #[error("字段 {0} 超出允许范围")]
    FieldOutOfRange(&'static str),
    #[error("长度算术溢出")]
    LengthOverflow,
    #[error("文件长度 {got} 与头部声明的 {expected} 不一致")]
    LengthMismatch { got: usize, expected: usize },
    #[error("槽位数 {got} 与参数决定的 {expected} 不一致")]
    SlotsLenMismatch { got: usize, expected: usize },
    #[error("owners 偏移 {got} 与槽位段结束位置 {expected} 不一致")]
    OwnersOffsetMismatch { got: usize, expected: usize },
    #[error("指纹 {0} 超出位宽允许范围")]
    FingerprintOutOfRange(u32),
    #[error("校验和不匹配：存储 {stored:#018x}，实际 {actual:#018x}（文件可能已损坏）")]
    ChecksumMismatch { stored: u64, actual: u64 },
    #[error("非空槽缺少归属 jti")]
    SlotWithoutOwner,
    #[error("空槽却带归属 jti（槽位与归属不一致）")]
    OwnerWithoutSlot,
    #[error("归属 jti 重复，凭证与槽必须一一对应")]
    DuplicateOwner,
    #[error("头部占用计数 {header} 与非空槽统计 {counted} 不一致")]
    OccupiedMismatch { header: u64, counted: u64 },
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> Snapshot {
        let j1 = [1u8; 16];
        let z = [0u8; 16];
        Snapshot {
            buckets_exp: 1,
            bucket_size: 2,
            fp_bits: 8,
            max_kicks: 50,
            occupied: 1,
            slots: vec![1, 0, 0, 0],
            owners: vec![j1, z, z, z],
        }
    }

    #[test]
    fn roundtrip() {
        let s = sample();
        let buf = s.encode();
        assert_eq!(buf.len(), HEADER_LEN + 4 * 2 + 4 * OWNER_LEN + 8);
        let back = Snapshot::decode(&buf).unwrap();
        assert_eq!(back, s);
    }

    #[test]
    fn detects_corruption_and_truncation() {
        let buf = sample().encode();
        assert!(matches!(Snapshot::decode(&buf[..10]), Err(CodecError::Truncated(_))));
        let mut bad_magic = buf.clone();
        bad_magic[0] ^= 0xFF;
        assert!(matches!(Snapshot::decode(&bad_magic), Err(CodecError::BadMagic)));

        let mut bitflip = buf.clone();
        bitflip[HEADER_LEN] ^= 0x01;
        assert!(matches!(
            Snapshot::decode(&bitflip),
            Err(CodecError::ChecksumMismatch { .. })
        ));
    }

    #[test]
    fn owner_slot_alignment_enforced() {
        let mut s = sample();
        // 非空槽但 jti 全零。
        s.owners[0] = [0u8; 16];
        assert!(matches!(
            Snapshot::decode(&s.encode()),
            Err(CodecError::SlotWithoutOwner)
        ));

        let mut s = sample();
        // 空槽带 jti。
        s.owners[1] = [9u8; 16];
        assert!(matches!(
            Snapshot::decode(&s.encode()),
            Err(CodecError::OwnerWithoutSlot)
        ));

        let mut s = sample();
        // 两个槽同一 jti。
        s.slots[1] = 2;
        s.owners[1] = [1u8; 16];
        s.occupied = 2;
        assert!(matches!(
            Snapshot::decode(&s.encode()),
            Err(CodecError::DuplicateOwner)
        ));

        let mut s = sample();
        s.occupied = 3;
        assert!(matches!(
            Snapshot::decode(&s.encode()),
            Err(CodecError::OccupiedMismatch { .. })
        ));
    }

    #[test]
    fn fingerprint_out_of_range_rejected() {
        let mut s = sample();
        s.slots[0] = 300; // > 2^8 - 1
        s.occupied = 1;
        assert!(matches!(
            Snapshot::decode(&s.encode()),
            Err(CodecError::FingerprintOutOfRange(300))
        ));
    }
}
