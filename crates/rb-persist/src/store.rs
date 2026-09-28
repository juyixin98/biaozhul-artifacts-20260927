//! 基础目录上的集合存储（原子写 + 严格读）。

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use rb_format::{codec, CodecError, RoaringSet};

use crate::naming::{data_path, is_valid_name};

/// 临时文件后缀序号，配合纳秒时间戳保证同进程并发保存不碰撞。
static TMP_SEQ: AtomicU64 = AtomicU64::new(0);

/// 存储层错误（保留内部 [`CodecError`]，调用方可精确分类损坏原因）。
#[derive(Debug)]
pub enum StoreError {
    /// 集合名非法（路径逃逸风险）。
    InvalidName(String),
    /// 集合不存在。
    NotFound(String),
    /// 名称已存在（用于 create-if-absent 语义）。
    AlreadyExists(String),
    /// 二进制解码/校验失败（文件损坏或格式不符）。
    Codec(CodecError),
    /// 文件系统 I/O 错误。
    Io(std::io::Error),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::InvalidName(n) => write!(f, "invalid set name: {n:?}"),
            StoreError::NotFound(n) => write!(f, "set not found: {n}"),
            StoreError::AlreadyExists(n) => write!(f, "set already exists: {n}"),
            StoreError::Codec(e) => write!(f, "corrupt data for set: {e}"),
            StoreError::Io(e) => write!(f, "filesystem error: {e}"),
        }
    }
}

impl std::error::Error for StoreError {}

impl From<std::io::Error> for StoreError {
    fn from(e: std::io::Error) -> Self {
        StoreError::Io(e)
    }
}

impl From<CodecError> for StoreError {
    fn from(e: CodecError) -> Self {
        StoreError::Codec(e)
    }
}

/// 一个目录即一个集合命名空间。
#[derive(Debug, Clone)]
pub struct Store {
    base: PathBuf,
}

impl Store {
    /// 打开（必要时创建）基础目录。
    pub fn open(base: impl AsRef<Path>) -> std::result::Result<Self, StoreError> {
        let base = base.as_ref().to_path_buf();
        fs::create_dir_all(&base)?;
        Ok(Store { base })
    }

    /// 基础目录路径。
    pub fn base(&self) -> &Path {
        &self.base
    }

    fn path_of(&self, name: &str) -> std::result::Result<PathBuf, StoreError> {
        data_path(&self.base, name).ok_or_else(|| StoreError::InvalidName(name.to_string()))
    }

    /// 原子保存：写 `<name>.rbs.tmp-<pid>`，flush+sync 后 rename 覆盖。
    pub fn save(&self, name: &str, set: &RoaringSet) -> std::result::Result<(), StoreError> {
        let target = self.path_of(name)?;
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let seq = TMP_SEQ.fetch_add(1, Ordering::Relaxed);
        let tmp = self.base.join(format!("{name}.rbs.tmp-{}-{}", nanos, seq));
        {
            let data = codec::encode(set);
            let mut f = fs::File::create(&tmp)?;
            f.write_all(&data)?;
            f.flush()?;
            f.sync_all()?;
        }
        // rename 在同目录（同一文件系统）上是原子的。
        fs::rename(&tmp, &target)?;
        // 尽力 fsync 目录，使 rename 落盘（失败不致命，数据已在日志覆盖语义下可读）。
        if let Ok(dir) = fs::File::open(&self.base) {
            let _ = dir.sync_all();
        }
        Ok(())
    }

    /// 仅当名称不存在时保存。
    pub fn save_new(&self, name: &str, set: &RoaringSet) -> std::result::Result<(), StoreError> {
        let target = self.path_of(name)?;
        if target.exists() {
            return Err(StoreError::AlreadyExists(name.to_string()));
        }
        self.save(name, set)
    }

    /// 加载并严格校验。
    pub fn load(&self, name: &str) -> std::result::Result<RoaringSet, StoreError> {
        let target = self.path_of(name)?;
        match fs::read(&target) {
            Ok(bytes) => Ok(codec::decode(&bytes)?),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                Err(StoreError::NotFound(name.to_string()))
            }
            Err(e) => Err(StoreError::Io(e)),
        }
    }

    /// 删除集合。
    pub fn delete(&self, name: &str) -> std::result::Result<bool, StoreError> {
        let target = self.path_of(name)?;
        match fs::remove_file(&target) {
            Ok(()) => Ok(true),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(e) => Err(StoreError::Io(e)),
        }
    }

    /// 是否存在。
    pub fn exists(&self, name: &str) -> bool {
        self.path_of(name).map(|p| p.exists()).unwrap_or(false)
    }

    /// 枚举所有合法集合名（扫描 `.rbs` 后缀，忽略临时文件与其它文件）。
    pub fn list(&self) -> std::result::Result<Vec<String>, StoreError> {
        let mut names = Vec::new();
        for entry in fs::read_dir(&self.base)? {
            let entry = entry?;
            let fname = entry.file_name();
            let Some(fname) = fname.to_str() else {
                continue;
            };
            let Some(stem) = fname.strip_suffix(".rbs") else {
                continue;
            };
            if is_valid_name(stem) {
                names.push(stem.to_string());
            }
        }
        names.sort();
        Ok(names)
    }
}
