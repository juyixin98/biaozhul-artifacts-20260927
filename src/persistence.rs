//! Filesystem adapter: a directory of `<set>.mphf` files.
//!
//! Set names are restricted to a safe character set so they can never escape
//! the data directory via `..` or absolute paths. Writes are atomic
//! (temp file + rename, delegated to [`crate::format::save_to_path`]).

use std::path::{Path, PathBuf};
use std::sync::Arc;

use tokio::sync::RwLock;

use crate::error::{ErrorKind, MphfError, Result};
use crate::format;
use crate::index::MphfIndex;

/// Filesystem-backed set store.
#[derive(Clone)]
pub struct Store {
    data_dir: PathBuf,
    /// Simple in-memory cache of loaded indices.
    cache: Arc<RwLock<std::collections::HashMap<String, Arc<MphfIndex>>>>,
}

fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 128
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
        && !name.starts_with('.')
}

impl Store {
    pub fn new(data_dir: impl AsRef<Path>) -> Result<Store> {
        let data_dir = data_dir.as_ref().to_path_buf();
        std::fs::create_dir_all(&data_dir)?;
        Ok(Store {
            data_dir,
            cache: Arc::new(RwLock::new(std::collections::HashMap::new())),
        })
    }

    pub fn data_dir(&self) -> &Path {
        &self.data_dir
    }

    fn path_for(&self, name: &str) -> Result<PathBuf> {
        if !valid_name(name) {
            return Err(MphfError::invalid_input(format!(
                "invalid set name {name:?}: allowed [A-Za-z0-9-_.], no leading dot, <=128 chars"
            )));
        }
        Ok(self.data_dir.join(format!("{name}.mphf")))
    }

    /// Persist a freshly built index (and cache it).
    pub async fn save(&self, name: &str, index: MphfIndex) -> Result<()> {
        let path = self.path_for(name)?;
        let idx = Arc::new(index);
        format::save_to_path(&path, &idx)?;
        self.cache.write().await.insert(name.to_string(), idx);
        Ok(())
    }

    /// Load a set: cache, else disk. [`MphfError::SetNotFound`] when absent;
    /// format errors surface with their specific [`ErrorKind`].
    pub async fn load(&self, name: &str) -> Result<Arc<MphfIndex>> {
        let path = self.path_for(name)?;
        if let Some(idx) = self.cache.read().await.get(name) {
            return Ok(Arc::clone(idx));
        }
        if !path.exists() {
            return Err(MphfError::SetNotFound(name.to_string()));
        }
        // Blocking parse is brief for typical fixtures; run via spawn_blocking
        // to avoid stalling the runtime on large files.
        let p = path.clone();
        let idx = tokio::task::spawn_blocking(move || format::load_from_path(p))
            .await
            .map_err(|e| MphfError::Internal(format!("load task join error: {e}")))??;
        let idx = Arc::new(idx);
        self.cache
            .write()
            .await
            .insert(name.to_string(), Arc::clone(&idx));
        Ok(idx)
    }

    /// Drop the cached copy so the next load re-reads the file.
    pub async fn invalidate(&self, name: &str) {
        self.cache.write().await.remove(name);
    }

    /// List discoverable sets (files matching `*.mphf`).
    pub async fn list(&self) -> Result<Vec<String>> {
        let mut out = Vec::new();
        for entry in std::fs::read_dir(&self.data_dir)? {
            let entry = entry?;
            if let Some(fname) = entry.file_name().to_str() {
                if let Some(stem) = fname.strip_suffix(".mphf") {
                    if valid_name(stem) {
                        out.push(stem.to_string());
                    }
                }
            }
        }
        out.sort();
        Ok(out)
    }
}

/// Used by the HTTP layer to map storage errors cleanly.
pub fn classify(e: &MphfError) -> ErrorKind {
    e.kind()
}
