//! Persistence adapter: a filesystem-backed object store.
//!
//! Objects are stored as `<store_dir>/<id>.hcomp` and written atomically
//! (temp file + rename), so a crashed request never leaves a half-written
//! object. Ids are validated to be safe filesystem-relative identifiers —
//! absolute paths, path separators, `.`/`..` traversal and hidden names are
//! all refused.

use crate::error::{Error, ErrorKind, Result};
use async_trait::async_trait;
use std::path::{Path, PathBuf};
use tokio::fs;

/// Metadata returned for a stored object.
#[derive(Debug, Clone)]
pub struct ObjectInfo {
    /// Object id (file stem).
    pub id: String,
    /// File size in bytes.
    pub size: u64,
    /// Last-modified Unix epoch seconds, when available.
    pub modified_unix: i64,
}

/// Object storage backend.
#[async_trait]
pub trait ObjectStore: Send + Sync {
    /// Persist `bytes` under `id`. Fails with [`ErrorKind::AlreadyExists`]
    /// when the object exists and `overwrite` is false.
    async fn put(&self, id: &str, bytes: &[u8], overwrite: bool) -> Result<()>;
    /// Read the raw container bytes for `id`.
    async fn get(&self, id: &str) -> Result<Vec<u8>>;
    /// Stat an object without reading its payload.
    async fn stat(&self, id: &str) -> Result<ObjectInfo>;
    /// Delete an object.
    async fn delete(&self, id: &str) -> Result<()>;
    /// List all object ids (unsorted lower bound; result is sorted here).
    async fn list(&self) -> Result<Vec<ObjectInfo>>;
}

/// Validate an object id. Allowed: 1..=128 chars from `[A-Za-z0-9._-]`,
/// with the additional restrictions that it is not `.`/`..`, does not start
/// with a dot and is not empty.
pub fn validate_id(id: &str) -> Result<()> {
    let n = id.len();
    if n == 0 || n > 128 {
        return Err(Error::new(
            ErrorKind::InvalidId,
            "id must be 1..=128 characters long",
        ));
    }
    if id == "." || id == ".." {
        return Err(Error::new(
            ErrorKind::InvalidId,
            "id may not be '.' or '..'",
        ));
    }
    if id.starts_with('.') {
        return Err(Error::new(
            ErrorKind::InvalidId,
            "id may not start with a dot",
        ));
    }
    if !id
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-' || c == '.')
    {
        return Err(Error::new(
            ErrorKind::InvalidId,
            "id may contain only ASCII letters, digits, '.', '_' and '-'",
        ));
    }
    // Guard against names that would normalise across separators or carry
    // trailing dots (Windows-host-compatible safety).
    if id.contains("..") || id.ends_with('.') {
        return Err(Error::new(
            ErrorKind::InvalidId,
            "id may not contain '..' or end with '.'",
        ));
    }
    Ok(())
}

/// Filesystem implementation of [`ObjectStore`].
#[derive(Debug, Clone)]
pub struct FileSystemStore {
    root: PathBuf,
}

impl FileSystemStore {
    /// Construct under `root`, creating the directory if needed.
    pub async fn new(root: impl Into<PathBuf>) -> Result<Self> {
        let root = root.into();
        fs::create_dir_all(&root).await?;
        Ok(FileSystemStore { root })
    }

    fn path_of(&self, id: &str) -> Result<PathBuf> {
        validate_id(id)?;
        Ok(self.root.join(format!("{id}.hcomp")))
    }

    fn id_from_path(path: &Path) -> Option<String> {
        if path.extension()? != "hcomp" {
            return None;
        }
        Some(path.file_stem()?.to_str()?.to_string())
    }
}

#[async_trait]
impl ObjectStore for FileSystemStore {
    async fn put(&self, id: &str, bytes: &[u8], overwrite: bool) -> Result<()> {
        let target = self.path_of(id)?;
        let exists = fs::try_exists(&target).await?;
        if exists && !overwrite {
            return Err(Error::new(
                ErrorKind::AlreadyExists,
                format!("object {id:?} already exists"),
            ));
        }
        let tmp = self
            .root
            .join(format!(".{id}.hcomp.tmp-{}", std::process::id()));
        fs::write(&tmp, bytes).await?;
        // rename is atomic within one directory on POSIX.
        fs::rename(&tmp, &target).await.map_err(|e| {
            // Best-effort cleanup of the temp file.
            let _ = std::fs::remove_file(&tmp);
            Error::from(e)
        })?;
        Ok(())
    }

    async fn get(&self, id: &str) -> Result<Vec<u8>> {
        let path = self.path_of(id)?;
        let bytes = fs::read(&path).await?;
        Ok(bytes)
    }

    async fn stat(&self, id: &str) -> Result<ObjectInfo> {
        let path = self.path_of(id)?;
        let meta = fs::metadata(&path).await?;
        let modified_unix = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        Ok(ObjectInfo {
            id: id.to_string(),
            size: meta.len(),
            modified_unix,
        })
    }

    async fn delete(&self, id: &str) -> Result<()> {
        let path = self.path_of(id)?;
        fs::remove_file(&path).await?;
        Ok(())
    }

    async fn list(&self) -> Result<Vec<ObjectInfo>> {
        let mut entries = fs::read_dir(&self.root).await?;
        let mut out = Vec::new();
        while let Some(entry) = entries.next_entry().await? {
            let path = entry.path();
            let Some(id) = Self::id_from_path(&path) else {
                continue;
            };
            let meta = entry.metadata().await?;
            if !meta.is_file() {
                continue;
            }
            let modified_unix = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_secs() as i64)
                .unwrap_or(0);
            out.push(ObjectInfo {
                id,
                size: meta.len(),
                modified_unix,
            });
        }
        out.sort_by(|a, b| a.id.cmp(&b.id));
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn id_validation() {
        for good in ["a", "abc", "A1_b-2.c", "x".repeat(128).as_str()] {
            validate_id(good).expect(good);
        }
        for bad in [
            "",
            ".",
            "..",
            ".hidden",
            "a/b",
            "../etc",
            "a\\b",
            "a b",
            "a..b",
            "trailing.",
            "x".repeat(129).as_str(),
        ] {
            assert_eq!(
                validate_id(bad).unwrap_err().kind(),
                ErrorKind::InvalidId,
                "id {bad:?} should be rejected"
            );
        }
    }

    static RT: tokio::sync::OnceCell<()> = tokio::sync::OnceCell::const_new();

    async fn rt() {
        RT.get_or_init(|| async {}).await;
    }

    #[tokio::test]
    async fn put_get_list_delete_cycle() {
        rt().await;
        let dir = tempfile::tempdir().unwrap();
        let store = FileSystemStore::new(dir.path()).await.unwrap();
        store.put("alpha", b"hello", false).await.unwrap();
        assert_eq!(store.get("alpha").await.unwrap(), b"hello");
        assert_eq!(
            store.put("alpha", b"x", false).await.unwrap_err().kind(),
            ErrorKind::AlreadyExists
        );
        store.put("alpha", b"world", true).await.unwrap();
        assert_eq!(store.get("alpha").await.unwrap(), b"world");
        store.put("beta", b"bye", false).await.unwrap();
        let ids: Vec<String> = store
            .list()
            .await
            .unwrap()
            .into_iter()
            .map(|i| i.id)
            .collect();
        assert_eq!(ids, vec!["alpha", "beta"]);
        store.delete("alpha").await.unwrap();
        assert_eq!(
            store.get("alpha").await.unwrap_err().kind(),
            ErrorKind::NotFound
        );
        // Temp files never survive a successful write.
        let leftovers: Vec<_> = std::fs::read_dir(dir.path())
            .unwrap()
            .filter_map(std::result::Result::ok)
            .map(|e| e.file_name().to_string_lossy().to_string())
            .filter(|n| n.contains(".tmp-"))
            .collect();
        assert!(
            leftovers.is_empty(),
            "temp files left behind: {leftovers:?}"
        );
    }

    #[tokio::test]
    async fn traversal_ids_are_blocked_before_filesystem_access() {
        let dir = tempfile::tempdir().unwrap();
        let store = FileSystemStore::new(dir.path()).await.unwrap();
        assert_eq!(
            store
                .put("../escape", b"x", false)
                .await
                .unwrap_err()
                .kind(),
            ErrorKind::InvalidId
        );
        assert_eq!(
            store.get("a/b").await.unwrap_err().kind(),
            ErrorKind::InvalidId
        );
    }
}
