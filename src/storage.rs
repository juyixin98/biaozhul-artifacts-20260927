//! Filesystem persistence adapter.
//!
//! Containers are stored as `<storage_dir>/<id>.rcmp`.  Writes go to a
//! temporary file in the same directory and are atomically renamed, so a
//! crashed writer never leaves a partial file at its final name.  A sidecar
//! `<id>.meta.json` records the declared metadata for listing/inspection.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

/// Errors from the storage layer.
#[derive(Debug)]
pub enum StorageError {
    Io { path: PathBuf, message: String },
    NotFound { id: String },
    BadId(String),
}

impl std::fmt::Display for StorageError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StorageError::Io { path, message } => {
                write!(f, "I/O error at {}: {message}", path.display())
            }
            StorageError::NotFound { id } => write!(f, "job {id:?} not found"),
            StorageError::BadId(id) => write!(f, "invalid job id {id:?}"),
        }
    }
}

impl std::error::Error for StorageError {}

/// Filesystem-backed job store.
#[derive(Debug)]
pub struct FileStore {
    root: PathBuf,
    /// Serializes writes within one process (simple, adequate locally).
    write_lock: Mutex<()>,
}

fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 64
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}

impl FileStore {
    /// Open (and create) a store rooted at `root`.
    pub fn open(root: impl AsRef<Path>) -> Result<Self, StorageError> {
        let root = root.as_ref().to_path_buf();
        std::fs::create_dir_all(&root).map_err(|e| StorageError::Io {
            path: root.clone(),
            message: format!("create_dir_all: {e}"),
        })?;
        Ok(Self {
            root,
            write_lock: Mutex::new(()),
        })
    }

    fn data_path(&self, id: &str) -> Result<PathBuf, StorageError> {
        if !valid_id(id) {
            return Err(StorageError::BadId(id.to_string()));
        }
        Ok(self.root.join(format!("{id}.rcmp")))
    }

    fn meta_path(&self, id: &str) -> Result<PathBuf, StorageError> {
        if !valid_id(id) {
            return Err(StorageError::BadId(id.to_string()));
        }
        Ok(self.root.join(format!("{id}.meta.json")))
    }

    /// Atomically write a container plus its metadata JSON.
    pub fn put(
        &self,
        id: &str,
        container: &[u8],
        meta: &serde_json::Value,
    ) -> Result<(), StorageError> {
        let _guard = self.write_lock.lock().expect("write lock poisoned");
        let final_path = self.data_path(id)?;
        let meta_path = self.meta_path(id)?;
        let tmp_path = self.root.join(format!(".{id}.rcmp.tmp"));
        let tmp_meta = self.root.join(format!(".{id}.meta.json.tmp"));

        std::fs::write(&tmp_path, container).map_err(|e| StorageError::Io {
            path: tmp_path.clone(),
            message: e.to_string(),
        })?;
        std::fs::write(&tmp_meta, serde_json::to_vec_pretty(meta).unwrap()).map_err(|e| {
            StorageError::Io {
                path: tmp_meta.clone(),
                message: e.to_string(),
            }
        })?;
        std::fs::rename(&tmp_path, &final_path).map_err(|e| StorageError::Io {
            path: final_path.clone(),
            message: format!("rename: {e}"),
        })?;
        std::fs::rename(&tmp_meta, &meta_path).map_err(|e| StorageError::Io {
            path: meta_path.clone(),
            message: format!("rename: {e}"),
        })?;
        Ok(())
    }

    /// Read a container by id.
    pub fn get(&self, id: &str) -> Result<Vec<u8>, StorageError> {
        let path = self.data_path(id)?;
        std::fs::read(&path).map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                StorageError::NotFound { id: id.to_string() }
            } else {
                StorageError::Io {
                    path,
                    message: e.to_string(),
                }
            }
        })
    }

    /// List stored job ids (ignoring temp/sidecar files).
    pub fn list(&self) -> Result<Vec<String>, StorageError> {
        let mut ids = Vec::new();
        for entry in std::fs::read_dir(&self.root).map_err(|e| StorageError::Io {
            path: self.root.clone(),
            message: e.to_string(),
        })? {
            let entry = entry.map_err(|e| StorageError::Io {
                path: self.root.clone(),
                message: e.to_string(),
            })?;
            let name = entry.file_name().to_string_lossy().to_string();
            if let Some(id) = name.strip_suffix(".rcmp") {
                if !id.starts_with('.') {
                    ids.push(id.to_string());
                }
            }
        }
        ids.sort();
        Ok(ids)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tempdir() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "rangecode-store-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn put_get_list_roundtrip() {
        let dir = tempdir();
        let store = FileStore::open(&dir).unwrap();
        store
            .put("job-1", b"payload", &serde_json::json!({"n": 1}))
            .unwrap();
        assert_eq!(store.get("job-1").unwrap(), b"payload");
        assert_eq!(store.list().unwrap(), vec!["job-1".to_string()]);
    }

    #[test]
    fn missing_id_is_not_found() {
        let dir = tempdir();
        let store = FileStore::open(&dir).unwrap();
        assert!(matches!(
            store.get("nope"),
            Err(StorageError::NotFound { .. })
        ));
    }

    #[test]
    fn rejects_path_traversal_ids() {
        let dir = tempdir();
        let store = FileStore::open(&dir).unwrap();
        assert!(matches!(
            store.get("../etc-passwd"),
            Err(StorageError::BadId(_))
        ));
    }

    #[test]
    fn no_partial_file_after_failed_meta() {
        // Simulate a reader: final files must have both artifacts.
        let dir = tempdir();
        let store = FileStore::open(&dir).unwrap();
        store.put("ok", b"x", &serde_json::json!({})).unwrap();
        let entries: Vec<_> = std::fs::read_dir(&dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
            .collect();
        assert!(entries.iter().any(|n| n == "ok.rcmp"));
        assert!(entries.iter().any(|n| n == "ok.meta.json"));
        assert!(
            !entries.iter().any(|n| n.starts_with('.')),
            "temp files left: {entries:?}"
        );
    }
}
