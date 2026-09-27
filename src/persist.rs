//! 持久化适配：索引落盘 / 加载 / 损坏检测。
//!
//! ## 磁盘布局（每个索引一个目录）
//!
//! ```text
//! <data_dir>/<index_name>/
//!   manifest.json   最后写入；存在即代表本目录保存完整（崩溃恢复的提交点）
//!   occ.bin         BWT + rank 检查点（rank::Occ 自描述二进制）
//!   c.bin           C 表 257×u64
//!   samples.bin     u64 对数 + (row u64, sa u64)×k
//!   text.bin        原始字节文本（verify 朴素扫描与信息展示用）
//! ```
//!
//! ## 防损坏策略
//! 1. 保存走“暂存目录 + 逐文件 fsync + 整体 rename”，manifest 最后落盘，
//!    崩溃只会留下无 manifest 的暂存/残目录，加载时一律视为损坏而非部分可用。
//! 2. manifest 记录每个文件的 SHA-256 与字节长度；加载先核长度再核哈希，
//!    任何不符报 [`FmError::Corrupt`]，与“文件不存在”([`FmError::NotFound`]) 区分开。
//! 3. 二进制结构自校验（见 [`crate::rank::Occ::from_bytes`]），
//!    最后由 [`crate::fm::FmIndex::validate`] 做内核不变量终检。

use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::alphabet::ALPHABET_SIZE;
use crate::error::{FmError, Result};
use crate::fm::FmIndex;
use crate::rank::Occ;

const MANIFEST: &str = "manifest.json";
const FILE_OCC: &str = "occ.bin";
const FILE_C: &str = "c.bin";
const FILE_SAMPLES: &str = "samples.bin";
const FILE_TEXT: &str = "text.bin";

const FORMAT_VERSION: u32 = 1;

/// manifest.json 内容。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Manifest {
    pub format_version: u32,
    /// 编码后长度 n = text_len + 1。
    pub encoded_len: u64,
    pub text_len: u64,
    pub rank_block: u32,
    pub sample_step: u32,
    pub sample_count: u64,
    /// sa[i]=0 的后缀行（哨兵在 BWT 中的唯一位置）。
    pub sentinel_row: u64,
    /// 秒级 Unix 时间戳（构建时间）。
    pub created_at: u64,
    pub files: FileHashes,
}

/// 每个数据文件的 SHA-256（十六进制）与字节长度。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct FileHashes {
    pub occ: FileHash,
    pub c: FileHash,
    pub samples: FileHash,
    pub text: FileHash,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct FileHash {
    pub sha256: String,
    pub len: u64,
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    hex::encode(h.finalize())
}

/// 读文件并校验长度与 SHA-256；文件缺失按 [`FmError::Corrupt`]（manifest 引用了它）。
fn read_verified(dir: &Path, name: &str, expect: &FileHash) -> Result<Vec<u8>> {
    let path = dir.join(name);
    let bytes = fs::read(&path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            FmError::corrupt(format!("索引数据文件 {name} 缺失"))
        } else {
            FmError::from(e)
        }
    })?;
    if bytes.len() as u64 != expect.len {
        return Err(FmError::corrupt(format!(
            "{name} 长度 {} 与 manifest 记录 {} 不符",
            bytes.len(),
            expect.len
        )));
    }
    let actual = sha256_hex(&bytes);
    if actual != expect.sha256 {
        return Err(FmError::corrupt(format!(
            "{name} SHA-256 校验失败（期望 {}，实际 {actual}）",
            expect.sha256
        )));
    }
    Ok(bytes)
}

fn now_unix_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// 索引目录路径。
pub fn index_dir(data_dir: &Path, name: &str) -> PathBuf {
    data_dir.join(name)
}

/// 数据目录是否已有该索引（以 manifest 为准）。
pub fn exists(data_dir: &Path, name: &str) -> bool {
    index_dir(data_dir, name).join(MANIFEST).is_file()
}

/// 原子保存索引。目标目录已存在（含 manifest）时报 [`FmError::StateConflict`]。
pub fn save(data_dir: &Path, name: &str, index: &FmIndex) -> Result<Manifest> {
    let final_dir = index_dir(data_dir, name);
    if final_dir.join(MANIFEST).exists() {
        return Err(FmError::state_conflict(format!("索引 {name} 已存在")));
    }

    fs::create_dir_all(data_dir)?;
    // 唯一暂存目录，避免并发构建互相踩踏。
    let staging = data_dir.join(format!(".tmp-{name}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&staging);
    fs::create_dir_all(&staging)?;

    let result = (|| -> Result<Manifest> {
        let occ_bytes = index.occ().to_bytes();

        let mut c_bytes = Vec::with_capacity(ALPHABET_SIZE * 8);
        for v in index.c_table() {
            c_bytes.extend_from_slice(&v.to_le_bytes());
        }

        let pairs = index.sample_pairs();
        let mut sample_bytes = Vec::with_capacity(8 + pairs.len() * 16);
        sample_bytes.extend_from_slice(&(pairs.len() as u64).to_le_bytes());
        for (row, sa) in &pairs {
            sample_bytes.extend_from_slice(&row.to_le_bytes());
            sample_bytes.extend_from_slice(&(*sa as u64).to_le_bytes());
        }

        let text_bytes = index.text();

        let write_one = |fname: &str, bytes: &[u8]| -> std::io::Result<()> {
            let tmp = staging.join(fname);
            fs::write(&tmp, bytes)?;
            // fsync 保证 rename 前数据落盘。
            #[cfg(unix)]
            fsync_path(&tmp)?;
            Ok(())
        };
        write_one(FILE_OCC, &occ_bytes)?;
        write_one(FILE_C, &c_bytes)?;
        write_one(FILE_SAMPLES, &sample_bytes)?;
        write_one(FILE_TEXT, text_bytes)?;
        // 暂存目录 fsync，保证 rename 提交后文件内容不丢。
        fsync_dir(&staging)?;

        let manifest = Manifest {
            format_version: FORMAT_VERSION,
            encoded_len: index.encoded_len(),
            text_len: index.text_len(),
            rank_block: index.rank_block(),
            sample_step: index.sample_step(),
            sample_count: pairs.len() as u64,
            sentinel_row: index.sentinel_row(),
            created_at: now_unix_secs(),
            files: FileHashes {
                occ: FileHash {
                    sha256: sha256_hex(&occ_bytes),
                    len: occ_bytes.len() as u64,
                },
                c: FileHash {
                    sha256: sha256_hex(&c_bytes),
                    len: c_bytes.len() as u64,
                },
                samples: FileHash {
                    sha256: sha256_hex(&sample_bytes),
                    len: sample_bytes.len() as u64,
                },
                text: FileHash {
                    sha256: sha256_hex(text_bytes),
                    len: text_bytes.len() as u64,
                },
            },
        };

        let man_bytes = serde_json::to_vec_pretty(&manifest)
            .map_err(|e| FmError::computation_failed(format!("manifest 序列化失败: {e}")))?;
        fs::write(staging.join(MANIFEST), &man_bytes)?;

        // 整体提交：rename(staging, final)。final 在入口已确认无 manifest；
        // 若目录残骸存在（无 manifest），先删除再 rename。
        if final_dir.exists() {
            fs::remove_dir_all(&final_dir)?;
        }
        fs::rename(&staging, &final_dir)?;
        Ok(manifest)
    })();

    if result.is_err() {
        let _ = fs::remove_dir_all(&staging);
    }
    result
}

#[cfg(unix)]
unsafe extern "C" {
    fn fsync(fd: i32) -> i32;
}

#[cfg(unix)]
use std::os::unix::io::AsRawFd;

/// 对已关闭/现存路径打开并 fsync。
#[cfg(unix)]
fn fsync_path(path: &Path) -> std::io::Result<()> {
    let f = fs::OpenOptions::new().read(true).open(path)?;
    let rc = unsafe { fsync(f.as_raw_fd()) };
    if rc == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

/// fsync 目录本身，保证其中目录项（含 rename）落盘。
#[cfg(unix)]
fn fsync_dir(path: &Path) -> std::io::Result<()> {
    use std::os::unix::ffi::OsStrExt;
    let p = std::ffi::CString::new(path.as_os_str().as_bytes())
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidInput, e))?;
    let fd = unsafe { libc_open_dir(&p) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error());
    }
    let rc = unsafe { fsync(fd) };
    let err = if rc != 0 {
        Some(std::io::Error::last_os_error())
    } else {
        None
    };
    unsafe { libc_close(fd) };
    match err {
        Some(e) => Err(e),
        None => Ok(()),
    }
}

#[cfg(unix)]
unsafe extern "C" {
    fn open(path: *const std::os::raw::c_char, oflag: i32, ...) -> i32;
    fn close(fd: i32) -> i32;
}

#[cfg(unix)]
unsafe fn libc_open_dir(p: &std::ffi::CString) -> i32 {
    // O_RDONLY | O_DIRECTORY = 0 | 0200000 (Linux)
    unsafe { open(p.as_ptr(), 0o200_000) }
}

#[cfg(unix)]
unsafe fn libc_close(fd: i32) {
    unsafe {
        let _ = close(fd);
    }
}

#[cfg(not(unix))]
fn fsync_path(_path: &Path) -> std::io::Result<()> {
    Ok(())
}

#[cfg(not(unix))]
fn fsync_dir(_path: &Path) -> std::io::Result<()> {
    Ok(())
}

/// 读取并解析 manifest（不校验数据文件）。manifest 缺失即索引不存在。
pub fn read_manifest(data_dir: &Path, name: &str) -> Result<Manifest> {
    let path = index_dir(data_dir, name).join(MANIFEST);
    let bytes = fs::read(&path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            FmError::not_found(format!("索引 {name} 不存在（manifest 缺失）"))
        } else {
            FmError::from(e)
        }
    })?;
    serde_json::from_slice::<Manifest>(&bytes)
        .map_err(|e| FmError::corrupt(format!("manifest.json 无法解析: {e}")))
}

/// 完整加载索引：manifest -> 哈希校验 -> 结构解析 -> 内核不变量。
pub fn load(data_dir: &Path, name: &str) -> Result<(FmIndex, Manifest)> {
    let dir = index_dir(data_dir, name);
    let manifest = read_manifest(data_dir, name)?;
    if manifest.format_version != FORMAT_VERSION {
        return Err(FmError::corrupt(format!(
            "format_version {} 不受支持（期望 {FORMAT_VERSION}）",
            manifest.format_version
        )));
    }

    let occ_bytes = read_verified(&dir, FILE_OCC, &manifest.files.occ)?;
    let c_bytes = read_verified(&dir, FILE_C, &manifest.files.c)?;
    let samples_bytes = read_verified(&dir, FILE_SAMPLES, &manifest.files.samples)?;
    let text_bytes = read_verified(&dir, FILE_TEXT, &manifest.files.text)?;

    // ---- C 表 ----
    if c_bytes.len() != ALPHABET_SIZE * 8 {
        return Err(FmError::corrupt(format!(
            "c.bin 长度 {} 非 {}",
            c_bytes.len(),
            ALPHABET_SIZE * 8
        )));
    }
    let mut c = [0u64; ALPHABET_SIZE];
    let (cchunks, crem) = c_bytes.as_chunks::<8>();
    if !crem.is_empty() {
        return Err(FmError::corrupt("c.bin 长度不是 8 的倍数"));
    }
    for (i, chunk) in cchunks.iter().enumerate() {
        c[i] = u64::from_le_bytes(*chunk);
    }

    // ---- 采样 ----
    if samples_bytes.len() < 8 {
        return Err(FmError::corrupt("samples.bin 过短，缺少计数"));
    }
    let count = u64::from_le_bytes(samples_bytes[..8].try_into().unwrap()) as usize;
    if samples_bytes.len() != 8 + count * 16 {
        return Err(FmError::corrupt("samples.bin 长度与采样计数不符"));
    }
    let mut sample_rows = Vec::with_capacity(count);
    let mut sample_sa = Vec::with_capacity(count);
    let (schunks, srem) = samples_bytes[8..].as_chunks::<16>();
    if !srem.is_empty() {
        return Err(FmError::corrupt("samples.bin 记录体不是 16 的倍数"));
    }
    for chunk in schunks {
        sample_rows.push(u64::from_le_bytes(chunk[0..8].try_into().unwrap()));
        let sa = u64::from_le_bytes(chunk[8..16].try_into().unwrap());
        if sa > u32::MAX as u64 {
            return Err(FmError::corrupt(format!(
                "samples.bin 中 sa 值 {sa} 超出 u32 寻址范围"
            )));
        }
        sample_sa.push(sa as u32);
    }
    if manifest.sample_count != count as u64 {
        return Err(FmError::corrupt(
            "manifest 的 sample_count 与 samples.bin 不一致",
        ));
    }

    // ---- 文本 / 长度 ----
    if text_bytes.len() as u64 != manifest.text_len {
        return Err(FmError::corrupt("text.bin 长度与 manifest 不符"));
    }
    if manifest.encoded_len != manifest.text_len + 1 {
        return Err(FmError::corrupt("manifest 中 encoded_len != text_len+1"));
    }

    // ---- occ（内含 BWT 自校验）----
    let occ = Occ::from_bytes(&occ_bytes)?;

    let index = FmIndex::from_parts(
        manifest.encoded_len,
        occ,
        c,
        sample_rows,
        sample_sa,
        manifest.sample_step,
        manifest.sentinel_row,
        text_bytes,
    )?;

    // 参数回读交叉检查
    if index.rank_block() != manifest.rank_block {
        return Err(FmError::corrupt("rank_block 与 manifest 不符"));
    }
    Ok((index, manifest))
}

/// 列出 data_dir 下所有含合法 manifest 的索引名（目录名排序）。
pub fn list(data_dir: &Path) -> Vec<String> {
    let mut names = Vec::new();
    let entries = match fs::read_dir(data_dir) {
        Ok(e) => e,
        Err(_) => return names,
    };
    for entry in entries.flatten() {
        if !entry.path().is_dir() {
            continue;
        }
        let name = match entry.file_name().into_string() {
            Ok(n) => n,
            Err(_) => continue,
        };
        if name.starts_with('.') {
            continue; // 暂存/隐藏目录
        }
        if entry.path().join(MANIFEST).is_file() {
            names.push(name);
        }
    }
    names.sort();
    names
}

/// 删除索引目录。不存在报 [`FmError::NotFound`]。
pub fn remove(data_dir: &Path, name: &str) -> Result<()> {
    let dir = index_dir(data_dir, name);
    if !dir.join(MANIFEST).exists() {
        return Err(FmError::not_found(format!("索引 {name} 不存在")));
    }
    fs::remove_dir_all(&dir)?;
    Ok(())
}
