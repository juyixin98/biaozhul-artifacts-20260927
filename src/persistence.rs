//! 单文件原子快照持久化。
//!
//! # 快照布局（全部小端；v1）
//!
//! ```text
//! 偏移  长度  字段
//! 0     8     magic = "CUCKOO1\n"
//! 8     4     format_version (u32 = 1)
//! 12    4     kernel_version  (u32)
//! 16    8     num_buckets     (u64)
//! 24    4     bucket_size     (u32)
//! 28    4     fingerprint_bits(u32)
//! 32    4     max_kicks       (u32)
//! 36    4     reserved (=0)
//! 40    32    kernel_seed
//! 72    8     occupied_cells (u64，非空槽数)
//! 80    8     total_copies   (u64，全部单元 copies 之和 = 记账存活总数)
//! 88    8     insert_ok       (u64，成功插入计数)
//! 96    8     insert_dup      (u64，重复插入计数)
//! 104   8     insert_full     (u64，容量耗尽计数)
//! 112   8     delete_ok       (u64)
//! 120   8     delete_denied   (u64，凭证失败计数)
//! 128   8     created_unix_ms (u64)
//! 136   8     updated_unix_ms (u64)
//! 144   8     ledger_bytes    (u64, L)
//! 152   4     crc32(header)   (对 0..152 的 CRC-32/ISO-HDLC)
//! 156   L     ledger blob
//! 156+L ...   cells：num_buckets*bucket_size 个单元，每个 40 字节
//!             (u32le fingerprint || u32le copies || owner[32])
//! 末 32       sha256(0..(len-32))
//! ```
//!
//! ledger blob 为自描述 JSON（`Ledger`）。槽表是定长二进制以控制体积；尾部 SHA-256
//! 检测任何静默损坏，头部 CRC 让「关键参数被翻转」在早期以明确错误暴露。
//!
//! 写入是崩溃安全的：写同目录临时文件 -> fsync 文件 -> rename -> fsync 目录。
//! 任何一步失败都不动旧快照。

use std::io::Write;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::filter::{CuckooFilter, CELL_BYTES};
use crate::hashing::KernelParams;
use crate::ledger::Ledger;
use crate::{SNAPSHOT_FORMAT_VERSION, TOKEN_VERSION};

/// 头部固定长度（不含 CRC 与载荷）。
const HEADER_LEN: usize = 152;
const CRC_LEN: usize = 4;
/// magic 8 字节。
const MAGIC: &[u8; 8] = b"CUCKOO1\n";

/// 与快照一同持久化的运行计数（单调累计）。
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Counters {
    pub insert_ok: u64,
    pub insert_dup: u64,
    pub insert_full: u64,
    pub delete_ok: u64,
    pub delete_denied: u64,
    pub created_unix_ms: u64,
    pub updated_unix_ms: u64,
}

/// 持久化/加载错误（区分损坏与 IO，调用方据此决定拒绝启动还是 500）。
#[derive(Debug)]
pub enum PersistError {
    Io(String),
    Corrupt(String),
}

impl std::fmt::Display for PersistError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PersistError::Io(s) => write!(f, "快照 IO 失败: {s}"),
            PersistError::Corrupt(s) => write!(f, "快照损坏或参数不匹配: {s}"),
        }
    }
}
impl std::error::Error for PersistError {}

/// 从快照解出的完整状态。
#[derive(Debug)]
pub struct Loaded {
    pub params: KernelParams,
    pub filter: CuckooFilter,
    pub ledger: Ledger,
    pub counters: Counters,
}

#[allow(clippy::too_many_arguments)]
fn build_header(
    params: &KernelParams,
    occupied: u64,
    total_copies: u64,
    c: &Counters,
    ledger_bytes: u64,
) -> Vec<u8> {
    let mut h = Vec::with_capacity(HEADER_LEN);
    h.extend_from_slice(MAGIC);
    h.extend_from_slice(&SNAPSHOT_FORMAT_VERSION.to_le_bytes());
    h.extend_from_slice(&params.version.to_le_bytes());
    h.extend_from_slice(&params.num_buckets.to_le_bytes());
    h.extend_from_slice(&params.bucket_size.to_le_bytes());
    h.extend_from_slice(&params.fingerprint_bits.to_le_bytes());
    h.extend_from_slice(&params.max_kicks.to_le_bytes());
    h.extend_from_slice(&0u32.to_le_bytes()); // reserved
    h.extend_from_slice(&params.seed);
    h.extend_from_slice(&occupied.to_le_bytes());
    h.extend_from_slice(&total_copies.to_le_bytes());
    h.extend_from_slice(&c.insert_ok.to_le_bytes());
    h.extend_from_slice(&c.insert_dup.to_le_bytes());
    h.extend_from_slice(&c.insert_full.to_le_bytes());
    h.extend_from_slice(&c.delete_ok.to_le_bytes());
    h.extend_from_slice(&c.delete_denied.to_le_bytes());
    h.extend_from_slice(&c.created_unix_ms.to_le_bytes());
    h.extend_from_slice(&c.updated_unix_ms.to_le_bytes());
    h.extend_from_slice(&ledger_bytes.to_le_bytes());
    debug_assert_eq!(h.len(), HEADER_LEN);
    h
}

/// 构造完整快照字节。
pub fn encode_snapshot(
    params: &KernelParams,
    filter: &CuckooFilter,
    ledger: &Ledger,
    counters: &Counters,
) -> Result<Vec<u8>, PersistError> {
    ledger.verify_invariants().map_err(PersistError::Corrupt)?;
    // 交叉不变式：桶表副本总数必须与记账存活总数一致。
    if filter.total_copies() != ledger.total_live() {
        return Err(PersistError::Corrupt(format!(
            "桶表副本总数 {} 与记账存活总数 {} 不一致",
            filter.total_copies(),
            ledger.total_live()
        )));
    }
    let ledger_json = serde_json::to_vec(ledger).map_err(|e| PersistError::Io(e.to_string()))?;

    let mut out = Vec::new();
    let header = build_header(
        params,
        filter.occupied_slots(),
        filter.total_copies(),
        counters,
        ledger_json.len() as u64,
    );
    out.extend_from_slice(&header);
    out.extend_from_slice(&crc32_ieee(&header).to_le_bytes());
    out.extend_from_slice(&ledger_json);
    out.extend_from_slice(&filter.encode_cells());

    let mut hasher = Sha256::new();
    hasher.update(&out);
    out.extend_from_slice(&hasher.finalize());
    Ok(out)
}

/// 解码并严格校验快照。`expect` 给出当前运行配置的内核参数，任何不一致都报错。
pub fn decode_snapshot(bytes: &[u8], expect: &KernelParams) -> Result<Loaded, PersistError> {
    let need_min = HEADER_LEN + CRC_LEN + 32;
    if bytes.len() < need_min {
        return Err(PersistError::Corrupt(format!(
            "长度 {} 小于最小快照 {need_min}",
            bytes.len()
        )));
    }
    if &bytes[0..8] != MAGIC {
        return Err(PersistError::Corrupt("magic 不匹配".into()));
    }
    let rd_u32 = |o: usize| u32::from_le_bytes(bytes[o..o + 4].try_into().unwrap());
    let rd_u64 = |o: usize| u64::from_le_bytes(bytes[o..o + 8].try_into().unwrap());

    let stored_crc = rd_u32(HEADER_LEN);
    let actual_crc = crc32_ieee(&bytes[0..HEADER_LEN]);
    if stored_crc != actual_crc {
        return Err(PersistError::Corrupt(format!(
            "头部 CRC 不匹配：存储 {stored_crc:#010x}，计算 {actual_crc:#010x}（参数区可能被篡改/损坏）"
        )));
    }

    // 尾部 SHA-256 先验：任何载荷损坏一律拒绝。
    let body_end = bytes.len() - 32;
    let mut hasher = Sha256::new();
    hasher.update(&bytes[0..body_end]);
    let digest: [u8; 32] = hasher.finalize().into();
    if digest != bytes[body_end..] {
        return Err(PersistError::Corrupt("整体 SHA-256 校验失败".into()));
    }

    let fmt_ver = rd_u32(8);
    if fmt_ver != SNAPSHOT_FORMAT_VERSION {
        return Err(PersistError::Corrupt(format!(
            "快照格式版本 {fmt_ver} 与本版本 {SNAPSHOT_FORMAT_VERSION} 不兼容"
        )));
    }

    let params = KernelParams {
        version: rd_u32(12),
        num_buckets: rd_u64(16),
        bucket_size: rd_u32(24),
        fingerprint_bits: rd_u32(28),
        max_kicks: rd_u32(32),
        seed: bytes[40..72].try_into().unwrap(),
    };
    if rd_u32(36) != 0 {
        return Err(PersistError::Corrupt("保留字段非零".into()));
    }
    if params != *expect {
        return Err(PersistError::Corrupt(format!(
            "快照内核参数与当前配置不一致：快照={:?}，配置={:?}（不允许隐式改参，请显式迁移）",
            params, expect
        )));
    }

    let occupied = rd_u64(72);
    let total_copies = rd_u64(80);
    let counters = Counters {
        insert_ok: rd_u64(88),
        insert_dup: rd_u64(96),
        insert_full: rd_u64(104),
        delete_ok: rd_u64(112),
        delete_denied: rd_u64(120),
        created_unix_ms: rd_u64(128),
        updated_unix_ms: rd_u64(136),
    };
    let ledger_bytes = rd_u64(144) as usize;

    let ledger_start = HEADER_LEN + CRC_LEN;
    let cells_start = ledger_start + ledger_bytes;
    let cells_bytes = (params.num_buckets as usize)
        .checked_mul(params.bucket_size as usize)
        .and_then(|n| n.checked_mul(CELL_BYTES))
        .ok_or_else(|| PersistError::Corrupt("槽区长度溢出".into()))?;
    if cells_start + cells_bytes != body_end {
        return Err(PersistError::Corrupt(format!(
            "长度不自洽：ledger={ledger_bytes} cells={cells_bytes}，实际载荷 {}",
            body_end - ledger_start
        )));
    }

    let ledger: Ledger = serde_json::from_slice(&bytes[ledger_start..cells_start])
        .map_err(|e| PersistError::Corrupt(format!("ledger JSON 解码失败: {e}")))?;
    ledger.verify_invariants().map_err(PersistError::Corrupt)?;

    let cells = CuckooFilter::decode_cells(&bytes[cells_start..body_end])
        .map_err(|e| PersistError::Corrupt(e.to_string()))?;
    let filter = CuckooFilter::from_cells(params.clone(), cells, total_copies)
        .map_err(|e| PersistError::Corrupt(e.to_string()))?;
    if filter.occupied_slots() != occupied {
        return Err(PersistError::Corrupt(format!(
            "头部占用槽数 {occupied} 与实际非空单元 {} 不一致",
            filter.occupied_slots()
        )));
    }

    // 交叉不变式：记账总存活副本数必须等于桶表副本总数。
    let live = ledger.total_live();
    if live != filter.total_copies() {
        return Err(PersistError::Corrupt(format!(
            "记账存活副本 {live} 与桶表副本总数 {} 不一致",
            filter.total_copies()
        )));
    }

    Ok(Loaded {
        params,
        filter,
        ledger,
        counters,
    })
}

/// 原子写入快照。fsync 可控（测试提速；生产必须开启）。
pub fn write_snapshot_atomic(
    path: &Path,
    bytes: &[u8],
    fsync_file: bool,
) -> Result<(), PersistError> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| PersistError::Io(e.to_string()))?;
    }
    let tmp: PathBuf = path.with_extra_extension("tmp");
    {
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&tmp)
            .map_err(|e| PersistError::Io(format!("打开临时文件 {}: {e}", tmp.display())))?;
        f.write_all(bytes)
            .map_err(|e| PersistError::Io(e.to_string()))?;
        if fsync_file {
            f.sync_all().map_err(|e| PersistError::Io(e.to_string()))?;
        }
    }
    std::fs::rename(&tmp, path).map_err(|e| PersistError::Io(format!("rename: {e}")))?;
    if fsync_file {
        if let Some(dir) = path.parent() {
            if let Ok(df) = std::fs::File::open(dir) {
                let _ = df.sync_all(); // 目录 fsync 失败不致命（部分平台），但文件已落盘。
            }
        }
    }
    Ok(())
}

/// 读取 HMAC 密钥：配置显式给出则用之；否则从 `secret_path` 加载，不存在则生成 0600 文件。
pub fn load_or_create_signing_secret(
    configured_hex: &str,
    secret_path: &Path,
) -> Result<[u8; 32], PersistError> {
    if !configured_hex.trim().is_empty() {
        return crate::config::decode_hex_32(configured_hex.trim()).map_err(PersistError::Corrupt);
    }
    if let Ok(bytes) = std::fs::read(secret_path) {
        if bytes.len() != 32 {
            return Err(PersistError::Corrupt(format!(
                "密钥文件 {} 长度 {} 不是 32 字节",
                secret_path.display(),
                bytes.len()
            )));
        }
        let mut k = [0u8; 32];
        k.copy_from_slice(&bytes);
        return Ok(k);
    }
    // 生成：用 getrandom（与 rand 同后端）填充并以 0600 写入。
    let mut k = [0u8; 32];
    getrandom::getrandom(&mut k).map_err(|e| PersistError::Io(format!("生成密钥失败: {e}")))?;
    if let Some(dir) = secret_path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| PersistError::Io(e.to_string()))?;
    }
    use std::os::unix::fs::OpenOptionsExt;
    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(secret_path)
        .map_err(|e| PersistError::Io(format!("创建密钥文件: {e}")))?;
    f.write_all(&k)
        .map_err(|e| PersistError::Io(e.to_string()))?;
    f.sync_all().map_err(|e| PersistError::Io(e.to_string()))?;
    Ok(k)
}

// ---------------------------------------------------------------------------
// CRC-32/ISO-HDLC（与常见 .png/.zip CRC 相同：poly 0xEDB88320 反射、初始全 1、异或全 1）
// 表在首次使用时构建。独立于被测内核，属于通用编码。
// ---------------------------------------------------------------------------
fn crc_table() -> &'static [u32; 256] {
    use std::sync::OnceLock;
    static TABLE: OnceLock<[u32; 256]> = OnceLock::new();
    TABLE.get_or_init(|| {
        let mut t = [0u32; 256];
        let mut n = 0usize;
        while n < 256 {
            let mut c = n as u32;
            let mut k = 0;
            while k < 8 {
                c = if c & 1 != 0 {
                    0xEDB88320 ^ (c >> 1)
                } else {
                    c >> 1
                };
                k += 1;
            }
            t[n] = c;
            n += 1;
        }
        t
    })
}

fn crc32_ieee(data: &[u8]) -> u32 {
    let t = crc_table();
    let mut crc = 0xFFFF_FFFFu32;
    for &b in data {
        let idx = ((crc ^ b as u32) & 0xFF) as usize;
        crc = (crc >> 8) ^ t[idx];
    }
    crc ^ 0xFFFF_FFFF
}

/// 当前时间（毫秒）。
pub fn now_ms() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// 令牌版本导出（供服务元信息使用）。
pub fn token_version() -> u8 {
    TOKEN_VERSION
}

/// PathBuf 小工具：追加临时扩展名。
trait WithExtraExtension {
    fn with_extra_extension(&self, ext: &str) -> PathBuf;
}
impl WithExtraExtension for Path {
    fn with_extra_extension(&self, ext: &str) -> PathBuf {
        let mut s = self.as_os_str().to_owned();
        s.push(".");
        s.push(ext);
        PathBuf::from(s)
    }
}

#[cfg(test)]
mod tests {
    /// CRC-32/ISO-HDLC 标准校验向量："123456789" -> 0xCBF43926。
    /// 该期望值来自公开标准，不是被测代码生成的。
    #[test]
    fn crc32_matches_known_check_vector() {
        assert_eq!(super::crc32_ieee(b"123456789"), 0xCBF43926);
        assert_eq!(super::crc32_ieee(b""), 0x00000000);
    }
}
