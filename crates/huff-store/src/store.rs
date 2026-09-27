//! Content-addressed artifact store on the local filesystem.
//!
//! Layout:
//!
//! ```text
//! <data_dir>/
//!   index.json
//!   artifacts/<sha256>.hfc
//! ```
//!
//! Writes are atomic (temp file + rename) and the index is rewritten
//! atomically after each successful insertion. The store performs no network
//! access.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use huff_core::container::{decode_container, parse_container};

use crate::model::{ArtifactRecord, Index};
use crate::sha256::sha256_hex;

const INDEX_SCHEMA: u32 = 1;
const ARTIFACTS_DIR: &str = "artifacts";
const INDEX_FILE: &str = "index.json";

/// Errors raised by the persistence layer.
#[derive(Debug)]
pub enum StoreError {
    Io(io::Error),
    Json(serde_json::Error),
    /// The offered bytes are not a valid, decodable container.
    Invalid(String),
    /// No artifact with that id.
    NotFound(String),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::Io(e) => write!(f, "io error: {e}"),
            StoreError::Json(e) => write!(f, "index json error: {e}"),
            StoreError::Invalid(code) => write!(f, "invalid container: {code}"),
            StoreError::NotFound(id) => write!(f, "artifact not found: {id}"),
        }
    }
}

impl std::error::Error for StoreError {}

impl From<io::Error> for StoreError {
    fn from(e: io::Error) -> Self {
        StoreError::Io(e)
    }
}

impl From<serde_json::Error> for StoreError {
    fn from(e: serde_json::Error) -> Self {
        StoreError::Json(e)
    }
}

/// Local content-addressed store rooted at one directory.
pub struct Store {
    root: PathBuf,
}

impl Store {
    /// Open (and, if needed, create) a store rooted at `root`.
    pub fn open(root: impl AsRef<Path>) -> Result<Self, StoreError> {
        let root = root.as_ref().to_path_buf();
        fs::create_dir_all(root.join(ARTIFACTS_DIR))?;
        let store = Self { root };
        if !store.index_path().exists() {
            store.write_index(&Index { schema: INDEX_SCHEMA, artifacts: Vec::new() })?;
        } else {
            // Validate that the existing index parses; never silently ignore.
            store.read_index()?;
        }
        Ok(store)
    }

    fn index_path(&self) -> PathBuf {
        self.root.join(INDEX_FILE)
    }

    fn artifacts_dir(&self) -> PathBuf {
        self.root.join(ARTIFACTS_DIR)
    }

    fn read_index(&self) -> Result<Index, StoreError> {
        let bytes = fs::read(self.index_path())?;
        let index: Index = serde_json::from_slice(&bytes)?;
        if index.schema != INDEX_SCHEMA {
            return Err(StoreError::Invalid(format!(
                "unsupported index schema {}",
                index.schema
            )));
        }
        Ok(index)
    }

    fn write_index(&self, index: &Index) -> Result<(), StoreError> {
        let bytes = serde_json::to_vec_pretty(index)?;
        let tmp = self.root.join(format!(".{INDEX_FILE}.tmp"));
        fs::write(&tmp, bytes)?;
        fs::rename(tmp, self.index_path())?;
        Ok(())
    }

    fn artifact_path(&self, id: &str) -> PathBuf {
        self.artifacts_dir().join(format!("{id}.hfc"))
    }

    /// Validate, decode-check and persist a container; returns its record.
    /// Idempotent: storing the same bytes twice keeps the first label/record.
    pub fn put(&self, container: &[u8], label: Option<String>) -> Result<ArtifactRecord, StoreError> {
        // Full verification before touching disk.
        let info = parse_container(container)
            .map_err(|e| StoreError::Invalid(e.code().to_string()))?;
        // Decode end-to-end so nothing undecodable can ever be stored.
        let decoded = decode_container(container)
            .map_err(|e| StoreError::Invalid(e.code().to_string()))?;
        debug_assert_eq!(decoded.len() as u32, info.original_total);

        let id = sha256_hex(container);
        let path = self.artifact_path(&id);
        if !path.exists() {
            let tmp = self.artifacts_dir().join(format!(".{id}.hfc.tmp"));
            fs::write(&tmp, container)?;
            fs::rename(&tmp, &path)?;
        }

        let mut index = self.read_index()?;
        if let Some(existing) = index.artifacts.iter().find(|a| a.id == id) {
            return Ok(existing.clone());
        }
        let record = ArtifactRecord {
            id: id.clone(),
            path: format!("{ARTIFACTS_DIR}/{id}.hfc"),
            container_len: container.len() as u64,
            original_len: info.original_total as u64,
            block_count: info.block_count,
            format_version: info.version,
            created_unix: unix_now(),
            label,
        };
        index.artifacts.push(record.clone());
        index.schema = INDEX_SCHEMA;
        self.write_index(&index)?;
        Ok(record)
    }

    /// Read the raw container bytes for an id.
    pub fn get(&self, id: &str) -> Result<Vec<u8>, StoreError> {
        let _index = self.read_index()?;
        let path = self.artifact_path(sanitize_id(id)?);
        if !path.exists() {
            return Err(StoreError::NotFound(id.to_string()));
        }
        fs::read(&path).map_err(Into::into)
    }

    /// List all records.
    pub fn list(&self) -> Result<Vec<ArtifactRecord>, StoreError> {
        Ok(self.read_index()?.artifacts)
    }

    /// Root directory (exposed for tests/operators).
    pub fn root(&self) -> &Path {
        &self.root
    }
}

/// Only allow hex content ids through (prevents path traversal).
fn sanitize_id(id: &str) -> Result<&str, StoreError> {
    if id.len() == 64 && id.bytes().all(|b| b.is_ascii_hexdigit()) {
        Ok(id)
    } else {
        Err(StoreError::NotFound(id.to_string()))
    }
}

/// Unix seconds without an external time dependency.
#[cfg(target_os = "linux")]
fn unix_now() -> u64 {
    // CLOCK_REALTIME = 0; `timespec { sec: i64, nsec: i64 }`.
    let mut ts = libc_timespec { sec: 0, nsec: 0 };
    let rc = unsafe { libc_clock_gettime(0, &mut ts) };
    if rc == 0 && ts.sec >= 0 {
        ts.sec as u64
    } else {
        0
    }
}

#[cfg(not(target_os = "linux"))]
fn unix_now() -> u64 {
    0
}

#[repr(C)]
struct libc_timespec {
    sec: i64,
    nsec: i64,
}

#[cfg(target_os = "linux")]
unsafe extern "C" {
    #[link_name = "clock_gettime"]
    fn libc_clock_gettime(clock_id: i32, tp: *mut libc_timespec) -> i32;
}

#[cfg(test)]
mod tests {
    use super::*;
    use huff_core::container::encode_container;
    use huff_core::DEFAULT_BLOCK_SIZE;

    fn temp_dir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!(
            "huff-store-{tag}-{}-{}",
            std::process::id(),
            std::file!().replace('/', "_")
        ));
        let _ = fs::remove_dir_all(&d);
        d
    }

    #[test]
    fn put_get_list_roundtrip() {
        let root = temp_dir("put-get");
        let store = Store::open(&root).unwrap();
        let container = encode_container(b"persisted payload", DEFAULT_BLOCK_SIZE).unwrap();
        let rec = store.put(&container, Some("demo".to_string())).unwrap();
        assert_eq!(rec.original_len, 17);

        let again = store.put(&container, Some("ignored".to_string())).unwrap();
        assert_eq!(again.id, rec.id);
        assert_eq!(again.label.as_deref(), Some("demo"));
        assert_eq!(store.list().unwrap().len(), 1);

        let loaded = store.get(&rec.id).unwrap();
        assert_eq!(loaded, container);
        assert_eq!(decode_container(&loaded).unwrap(), b"persisted payload");
    }

    #[test]
    fn rejects_invalid_container() {
        let root = temp_dir("reject");
        let store = Store::open(&root).unwrap();
        let err = store.put(b"not a container at all", None).unwrap_err();
        assert!(matches!(err, StoreError::Invalid(_)));
        assert!(store.list().unwrap().is_empty());
    }

    #[test]
    fn unknown_id_and_traversal_are_not_found() {
        let root = temp_dir("nf");
        let store = Store::open(&root).unwrap();
        assert!(matches!(store.get("zzz"), Err(StoreError::NotFound(_))));
        assert!(matches!(store.get("../index"), Err(StoreError::NotFound(_))));
    }
}
