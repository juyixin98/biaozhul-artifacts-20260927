//! Filesystem-backed index store.
//!
//! Each index lives at `<data_dir>/<name>.wmi`. Writes are atomic: data is
//! flushed to `<name>.wmi.tmp-<pid>` and `rename`-d into place, so a crash
//! never leaves a half-written index. Names are restricted to a safe
//! charset to prevent path traversal.

use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use wm_core::WaveletMatrix;
use wm_format::{decode, encode};

/// Directory containing `<name>.wmi` index files.
#[derive(Debug, Clone)]
pub struct IndexStore {
    dir: PathBuf,
}

/// Persistence failure categories.
#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    /// Index name contains characters outside `[A-Za-z0-9_-]` (or empty).
    #[error("invalid index name {name:?}: allowed characters are A-Z a-z 0-9 _ -")]
    InvalidName { name: String },

    /// Requested index does not exist.
    #[error("index {name:?} not found")]
    NotFound { name: String },

    /// Index already exists and overwrite was not requested.
    #[error("index {name:?} already exists")]
    AlreadyExists { name: String },

    /// IO failure while touching the store.
    #[error("io error at {}: {source}", path.display())]
    Io { path: PathBuf, source: io::Error },

    /// Stored image failed format/structural validation.
    #[error("index {name:?} is unreadable: {source}")]
    Corrupt {
        name: String,
        source: wm_format::FormatError,
    },

    /// Index build rejected the input (e.g. empty value list).
    #[error("index {name:?} rejected input: {source}")]
    Kernel {
        name: String,
        source: wm_core::WmError,
    },
}

impl IndexStore {
    /// Open (and, if needed, create) a store directory.
    pub fn open(dir: impl AsRef<Path>) -> Result<Self, StoreError> {
        let dir = dir.as_ref().to_path_buf();
        fs::create_dir_all(&dir).map_err(|source| StoreError::Io {
            path: dir.clone(),
            source,
        })?;
        Ok(Self { dir })
    }

    /// Directory backing the store.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.dir
    }

    fn validate_name(name: &str) -> Result<(), StoreError> {
        let ok = !name.is_empty()
            && name.len() <= 128
            && name
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-');
        if ok {
            Ok(())
        } else {
            Err(StoreError::InvalidName {
                name: name.to_string(),
            })
        }
    }

    fn file_path(&self, name: &str) -> Result<PathBuf, StoreError> {
        Self::validate_name(name)?;
        Ok(self.dir.join(format!("{name}.wmi")))
    }

    /// Build and persist a new index.
    ///
    /// # Errors
    /// [`StoreError::AlreadyExists`] if the index exists and `overwrite` is
    /// false; kernel or IO errors otherwise.
    pub fn create_index(
        &self,
        name: &str,
        values: &[i64],
        overwrite: bool,
    ) -> Result<WaveletMatrix, StoreError> {
        let path = self.file_path(name)?;
        if path.exists() && !overwrite {
            return Err(StoreError::AlreadyExists {
                name: name.to_string(),
            });
        }
        let wm = WaveletMatrix::build(values).map_err(|source| StoreError::Kernel {
            name: name.to_string(),
            source,
        })?;
        self.write_atomic(&path, name, &encode(&wm))?;
        Ok(wm)
    }

    /// Load an index from disk.
    pub fn load(&self, name: &str) -> Result<WaveletMatrix, StoreError> {
        let path = self.file_path(name)?;
        let bytes = fs::read(&path).map_err(|source| {
            if source.kind() == io::ErrorKind::NotFound {
                StoreError::NotFound {
                    name: name.to_string(),
                }
            } else {
                StoreError::Io {
                    path: path.clone(),
                    source,
                }
            }
        })?;
        decode(&bytes).map_err(|source| StoreError::Corrupt {
            name: name.to_string(),
            source,
        })
    }

    /// Remove an index from disk.
    pub fn delete(&self, name: &str) -> Result<(), StoreError> {
        let path = self.file_path(name)?;
        match fs::remove_file(&path) {
            Ok(()) => Ok(()),
            Err(source) if source.kind() == io::ErrorKind::NotFound => Err(StoreError::NotFound {
                name: name.to_string(),
            }),
            Err(source) => Err(StoreError::Io { path, source }),
        }
    }

    /// List indexes with metadata read from their on-disk headers.
    ///
    /// Unreadable entries are returned under `corrupt` instead of aborting
    /// the listing; callers report them explicitly.
    pub fn list(&self) -> Result<Listing, StoreError> {
        let mut indexes = BTreeMap::new();
        let mut corrupt = Vec::new();
        for entry in fs::read_dir(&self.dir).map_err(|source| StoreError::Io {
            path: self.dir.clone(),
            source,
        })? {
            let entry = entry.map_err(|source| StoreError::Io {
                path: self.dir.clone(),
                source,
            })?;
            let path = entry.path();
            if path.extension().and_then(|s| s.to_str()) != Some("wmi") {
                continue;
            }
            let Some(name) = path.file_stem().and_then(|s| s.to_str()) else {
                continue;
            };
            match self.load(name) {
                Ok(wm) => {
                    indexes.insert(
                        name.to_string(),
                        IndexMeta {
                            name: name.to_string(),
                            len: wm.len(),
                            distinct_count: wm.distinct_count(),
                            bit_len: wm.bit_len(),
                            min: wm.distinct_values().first().copied().unwrap_or(0),
                            max: wm.distinct_values().last().copied().unwrap_or(0),
                            size_bytes: entry.metadata().map(|m| m.len()).unwrap_or(0),
                        },
                    );
                }
                Err(StoreError::Corrupt { .. }) => corrupt.push(name.to_string()),
                Err(e) => return Err(e),
            }
        }
        Ok(Listing { indexes, corrupt })
    }

    fn write_atomic(&self, path: &Path, name: &str, bytes: &[u8]) -> Result<(), StoreError> {
        let tmp = self
            .dir
            .join(format!("{}.wmi.tmp-{}", name, std::process::id()));
        fs::write(&tmp, bytes).map_err(|source| StoreError::Io {
            path: tmp.clone(),
            source,
        })?;
        // Best-effort durability before the rename.
        if let Ok(f) = fs::File::open(&tmp) {
            if let Err(source) = f.sync_all() {
                let _ = fs::remove_file(&tmp);
                return Err(StoreError::Io { path: tmp, source });
            }
        }
        fs::rename(&tmp, path).map_err(|source| {
            let _ = fs::remove_file(&tmp);
            StoreError::Io {
                path: path.to_path_buf(),
                source,
            }
        })?;
        Ok(())
    }
}

/// Metadata describing a stored index without holding its contents.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IndexMeta {
    pub name: String,
    pub len: usize,
    pub distinct_count: usize,
    pub bit_len: usize,
    pub min: i64,
    pub max: i64,
    pub size_bytes: u64,
}

/// Result of listing a store.
#[derive(Debug, Clone, Default)]
pub struct Listing {
    pub indexes: BTreeMap<String, IndexMeta>,
    pub corrupt: Vec<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn save_load_delete_roundtrip() {
        let dir = std::env::temp_dir().join(format!("wm-store-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let store = IndexStore::open(&dir).unwrap();

        let values = [9i64, -1, 9, 0, i64::MIN, 42, i64::MAX, -1];
        let wm = store.create_index("demo", &values, false).unwrap();
        let loaded = store.load("demo").unwrap();
        assert_eq!(loaded, wm);

        // The reloaded index answers identically to the freshly built one.
        for k in 0..values.len() as u64 {
            assert_eq!(
                loaded.quantile(0, values.len(), k).unwrap(),
                wm.quantile(0, values.len(), k).unwrap()
            );
        }
        // [-1, 42): -1 twice, 0 once, 9 twice (42 excluded by open upper bound).
        assert_eq!(loaded.range_count(0, values.len(), -1, 42).unwrap(), 5);

        let listing = store.list().unwrap();
        assert_eq!(listing.indexes.len(), 1);
        assert!(listing.corrupt.is_empty());
        let meta = &listing.indexes["demo"];
        assert_eq!(meta.len, 8);
        assert_eq!(meta.distinct_count, 6);
        assert_eq!(meta.min, i64::MIN);
        assert_eq!(meta.max, i64::MAX);

        assert!(matches!(
            store.create_index("demo", &values, false).unwrap_err(),
            StoreError::AlreadyExists { .. }
        ));
        assert!(store.create_index("demo", &[1], true).is_ok());

        store.delete("demo").unwrap();
        assert!(matches!(
            store.load("demo").unwrap_err(),
            StoreError::NotFound { .. }
        ));

        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn rejects_unsafe_names_and_empty_input() {
        let dir = std::env::temp_dir().join(format!("wm-store-test-bad-{}", std::process::id()));
        let store = IndexStore::open(&dir).unwrap();
        for bad in ["", "../escape", "a/b", "dot.name", "space x"] {
            assert!(matches!(
                store.create_index(bad, &[1], false).unwrap_err(),
                StoreError::InvalidName { .. }
            ));
        }
        assert!(matches!(
            store.create_index("ok-name_1", &[], false).unwrap_err(),
            StoreError::Kernel { .. }
        ));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn corrupt_file_is_listed_separately() {
        let dir =
            std::env::temp_dir().join(format!("wm-store-test-corrupt-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let store = IndexStore::open(&dir).unwrap();
        store.create_index("good", &[1, 2, 3], false).unwrap();
        fs::write(store.path().join("bad.wmi"), b"not an index").unwrap();

        let listing = store.list().unwrap();
        assert_eq!(listing.indexes.len(), 1);
        assert_eq!(listing.corrupt, vec!["bad".to_string()]);
        assert!(matches!(
            store.load("bad").unwrap_err(),
            StoreError::Corrupt { .. }
        ));
        fs::remove_dir_all(&dir).unwrap();
    }
}
