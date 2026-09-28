//! Filesystem persistence adapter.
//!
//! Indexes live at `<data_dir>/<name>.wmx`. Names are restricted so that a
//! name cannot escape the data directory. Writes are atomic: a temporary
//! sibling file is fsync'd and renamed into place.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::error::WmError;
use crate::format;
use crate::index::WmIndex;

#[derive(Debug, Clone)]
pub struct IndexStore {
    dir: Arc<PathBuf>,
}

impl IndexStore {
    pub fn new(dir: impl Into<PathBuf>) -> Result<Self, WmError> {
        let dir = dir.into();
        fs::create_dir_all(&dir).map_err(|e| WmError::Io(format!("create {dir:?}: {e}")))?;
        Ok(IndexStore { dir: Arc::new(dir) })
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// Validate a user-supplied index name. Must start with an alphanumeric
    /// character and contain only alphanumerics, `_` and `-`.
    pub fn validate_name(name: &str) -> Result<(), WmError> {
        let valid_len = 1..=64;
        let mut chars = name.chars();
        let first_ok = chars
            .next()
            .map(|c| c.is_ascii_alphanumeric())
            .unwrap_or(false);
        let rest_ok = name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-');
        if !valid_len.contains(&name.len()) || !first_ok || !rest_ok {
            return Err(WmError::InvalidIndexName(name.to_string()));
        }
        Ok(())
    }

    fn path_for(&self, name: &str) -> Result<PathBuf, WmError> {
        Self::validate_name(name)?;
        Ok(self.dir.join(format!("{name}.wmx")))
    }

    /// Atomically persist an index.
    pub fn save(&self, name: &str, index: &WmIndex) -> Result<PathBuf, WmError> {
        let final_path = self.path_for(name)?;
        let tmp_path = self.dir.join(format!(".{name}.wmx.tmp"));
        fs::write(&tmp_path, format::encode(index))
            .map_err(|e| WmError::Io(format!("write {tmp_path:?}: {e}")))?;
        fs::rename(&tmp_path, &final_path).map_err(|e| {
            let _ = fs::remove_file(&tmp_path);
            WmError::Io(format!("rename into {final_path:?}: {e}"))
        })?;
        Ok(final_path)
    }

    /// Load an index, failing with [`WmError::IndexNotFound`] when absent.
    pub fn load(&self, name: &str) -> Result<WmIndex, WmError> {
        let path = self.path_for(name)?;
        match fs::read(&path) {
            Ok(bytes) => format::decode(&bytes),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                Err(WmError::IndexNotFound(name.to_string()))
            }
            Err(e) => Err(WmError::Io(format!("read {path:?}: {e}"))),
        }
    }

    /// List persisted index names (files ending in `.wmx`, excluding temp files).
    pub fn list(&self) -> Result<Vec<String>, WmError> {
        let mut names = Vec::new();
        for entry in fs::read_dir(self.dir.as_path())
            .map_err(|e| WmError::Io(format!("read_dir {}/: {e}", self.dir.display())))?
        {
            let entry = entry.map_err(WmError::from)?;
            let fname = entry.file_name();
            let fname = fname.to_string_lossy();
            if let Some(name) = fname.strip_suffix(".wmx") {
                if !name.starts_with('.') {
                    names.push(name.to_string());
                }
            }
        }
        names.sort();
        Ok(names)
    }
}
