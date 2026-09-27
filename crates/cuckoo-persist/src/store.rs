//! 文件系统持久化适配：原子快照写入 + 安全装载。
//!
//! 写路径（崩溃安全的标准 rename 模式）：
//! 1. 写入同目录临时文件 `<name>.tmp-<pid>`；
//! 2. `fsync` 临时文件；
//! 3. `rename` 原子替换目标文件（同文件系统内 rename 为原子操作）；
//! 4. `fsync` 所在目录，保证替换项落盘。
//!
//! 读路径：文件不存在视为“空存储”（由调用方决定是否首次初始化）；
//! 任何损坏/截断/参数矛盾都返回明确错误，不返回半份数据。

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use crate::codec::{CodecError, Snapshot};

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("I/O 错误 ({path}): {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error(transparent)]
    Codec(#[from] CodecError),
}

pub type StoreResult<T> = Result<T, StoreError>;

/// 单文件快照存储。
pub struct FileStore {
    path: PathBuf,
}

impl FileStore {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// 装载快照。返回 `Ok(None)` 仅当目标文件不存在（首次启动）。
    pub fn load(&self) -> StoreResult<Option<Snapshot>> {
        match fs::read(&self.path) {
            Ok(buf) => Ok(Some(Snapshot::decode(&buf)?)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(source) => Err(StoreError::Io {
                path: self.path.clone(),
                source,
            }),
        }
    }

    /// 原子写入快照。成功返回前数据与目录项均已 fsync。
    pub fn save_atomic(&self, snap: &Snapshot) -> StoreResult<()> {
        if let Some(dir) = self.path.parent() {
            if !dir.as_os_str().is_empty() {
                fs::create_dir_all(dir).map_err(|source| StoreError::Io {
                    path: dir.to_path_buf(),
                    source,
                })?;
            }
        }
        let data = snap.encode();
        let tmp = self.tmp_path();

        let write_result: StoreResult<()> = (|| {
            let mut f = fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .open(&tmp)
                .map_err(|source| StoreError::Io {
                    path: tmp.clone(),
                    source,
                })?;
            f.write_all(&data).map_err(|source| StoreError::Io {
                path: tmp.clone(),
                source,
            })?;
            f.sync_all().map_err(|source| StoreError::Io {
                path: tmp.clone(),
                source,
            })?;
            Ok(())
        })();
        if let Err(e) = write_result {
            let _ = fs::remove_file(&tmp);
            return Err(e);
        }

        fs::rename(&tmp, &self.path).map_err(|source| {
            let _ = fs::remove_file(&tmp);
            StoreError::Io {
                path: self.path.clone(),
                source,
            }
        })?;

        // fsync 目录让 rename 持久化（非 Linux 或目录无法打开时不致命：数据文件已 fsync）。
        if let Some(dir) = self.path.parent() {
            if !dir.as_os_str().is_empty() {
                if let Ok(df) = fs::File::open(dir) {
                    let _ = df.sync_all();
                }
            }
        }
        Ok(())
    }

    fn tmp_path(&self) -> PathBuf {
        let mut name = self
            .path
            .file_name()
            .map(|s| s.to_owned())
            .unwrap_or_else(|| "snapshot.bin".into());
        name.push(format!(".tmp-{}-{:x}", std::process::id(), unique_suffix()));
        self.path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .map(|p| p.join(&name))
            .unwrap_or_else(|| PathBuf::from(&name))
    }
}

fn unique_suffix() -> u64 {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(0);
    N.fetch_add(1, Ordering::Relaxed)
        ^ std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn snap() -> Snapshot {
        Snapshot {
            buckets_exp: 1,
            bucket_size: 2,
            fp_bits: 4,
            max_kicks: 10,
            occupied: 1,
            slots: vec![7, 0, 0, 0],
            owners: vec![[1u8; 16], [0u8; 16], [0u8; 16], [0u8; 16]],
        }
    }

    #[test]
    fn missing_file_is_none() {
        let dir = tempdir().unwrap();
        let store = FileStore::new(dir.path().join("nope.bin"));
        assert!(store.load().unwrap().is_none());
    }

    #[test]
    fn save_then_load_roundtrip() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("state").join("filter.bin");
        let store = FileStore::new(&path);
        store.save_atomic(&snap()).unwrap();
        let back = store.load().unwrap().unwrap();
        assert_eq!(back, snap());
        // 覆盖写依然可装载。
        let mut s2 = snap();
        s2.slots = vec![1, 2, 0, 0];
        s2.owners = vec![[1u8; 16], [2u8; 16], [0u8; 16], [0u8; 16]];
        s2.occupied = 2;
        store.save_atomic(&s2).unwrap();
        assert_eq!(store.load().unwrap().unwrap(), s2);
        // 临时文件被清理。
        let leftovers: Vec<_> = std::fs::read_dir(dir.path().join("state"))
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.file_name())
            .collect();
        assert_eq!(leftovers, vec![std::ffi::OsString::from("filter.bin")]);
    }

    #[test]
    fn garbage_file_is_error_not_none() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("corrupt.bin");
        std::fs::write(&path, b"definitely not a cuckoo snapshot").unwrap();
        let store = FileStore::new(&path);
        assert!(store.load().is_err());
    }
}
