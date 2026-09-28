//! Filesystem persistence adapter: atomic write (tmp + rename) and load.

use std::path::{Path, PathBuf};

use crate::format;
use crate::kernel::MphIndex;

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("io error on {path}: {source}")]
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("invalid index file at {path}: {source}")]
    Format {
        path: PathBuf,
        source: format::FormatError,
    },
}

/// Persist `index` atomically: write to a sibling temp file, then rename.
pub fn save(index: &MphIndex, path: &Path) -> Result<(), StoreError> {
    let bytes = format::encode(index);
    let tmp = path.with_extension("mph.tmp");
    let io = |source: std::io::Error| StoreError::Io {
        path: path.to_path_buf(),
        source,
    };
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent).map_err(io)?;
        }
    }
    std::fs::write(&tmp, &bytes).map_err(io)?;
    std::fs::rename(&tmp, path).map_err(io)?;
    Ok(())
}

pub fn load(path: &Path) -> Result<MphIndex, StoreError> {
    let bytes = std::fs::read(path).map_err(|source| StoreError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    format::decode(&bytes).map_err(|source| StoreError::Format {
        path: path.to_path_buf(),
        source,
    })
}
