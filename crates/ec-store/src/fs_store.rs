//! Filesystem [`ObjectStore`] with per-file atomic writes.
//!
//! Commit rule: shards are written first (temp file → fsync → atomic rename),
//! the manifest is renamed last. A directory without a verified manifest is
//! therefore an incomplete object and never read as committed.
//!
//! Assumptions documented honestly:
//! - POSIX semantics (Linux target): `rename(2)` within one directory is
//!   atomic and replaces the destination. Directory fsync on commit is best
//!   effort (errors logged, not fatal) so the crate still works on
//!   ordinary local filesystems.
//! - No file locking; object ids are fresh UUIDs and `put_object` rejects an
//!   already-existing object directory.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use ec_core::error::EcError;
use ec_format::Manifest;

use crate::paths::{manifest_path, shard_path, shards_dir};
use crate::{ensure_safe_id, io_err, ObjectStore, ShardRead};

/// Rooted filesystem store.
#[derive(Debug, Clone)]
pub struct FileSystemStore {
    root: PathBuf,
    tmp_seq: std::sync::Arc<AtomicU64>,
}

impl FileSystemStore {
    /// Create (if needed) and open the root directory.
    pub fn open(root: impl Into<PathBuf>) -> Result<Self, EcError> {
        let root = root.into();
        fs::create_dir_all(&root)
            .map_err(|e| io_err("creating store root", &root, e))?;
        Ok(Self {
            root,
            tmp_seq: std::sync::Arc::new(AtomicU64::new(0)),
        })
    }

    fn unique_tmp(&self, target: &Path) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let seq = self.tmp_seq.fetch_add(1, Ordering::Relaxed);
        let name = format!(
            ".{}.tmp-{}-{}-{}",
            target.file_name().and_then(|s| s.to_str()).unwrap_or("file"),
            std::process::id(),
            nanos,
            seq
        );
        target.with_file_name(name)
    }

    /// Write bytes to a unique temp file, fsync it, then atomically rename
    /// over `target` (same directory → same filesystem, POSIX guarantee).
    fn atomic_write(&self, target: &Path, bytes: &[u8]) -> Result<(), EcError> {
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)
                .map_err(|e| io_err("creating parent dir", &parent.to_path_buf(), e))?;
        }
        let tmp = self.unique_tmp(target);
        {
            let mut f = fs::File::create(&tmp)
                .map_err(|e| io_err("creating temp file", &tmp, e))?;
            f.write_all(bytes)
                .map_err(|e| io_err("writing temp file", &tmp, e))?;
            f.sync_all()
                .map_err(|e| io_err("syncing temp file", &tmp, e))?;
        }
        fs::rename(&tmp, target).map_err(|e| {
            // Best-effort cleanup so a failed rename does not litter.
            let _ = fs::remove_file(&tmp);
            io_err("atomic rename", target, e)
        })?;
        if let Some(parent) = target.parent() {
            if let Ok(dir) = fs::File::open(parent) {
                let _ = dir.sync_all();
            }
        }
        Ok(())
    }
}

impl ObjectStore for FileSystemStore {
    fn put_object(
        &self,
        manifest: &Manifest,
        shards: &[Vec<u8>],
    ) -> Result<(), EcError> {
        ensure_safe_id(&manifest.object_id)?;
        if shards.len() != manifest.shard_count as usize {
            return Err(EcError::SizeMismatch {
                detail: format!(
                    "put_object: {} shard files but manifest declares {}",
                    shards.len(),
                    manifest.shard_count
                ),
            });
        }
        let object_dir = self.root.join(&manifest.object_id);
        if object_dir.exists() {
            return Err(EcError::Store(format!(
                "ALREADY_EXISTS: object {} is already present under {}",
                manifest.object_id,
                self.root.display()
            )));
        }
        // First ensure the object directory exists (its absence is what makes
        // this a new object); shards land before the manifest commit marker.
        fs::create_dir_all(shards_dir(&self.root, &manifest.object_id))
            .map_err(|e| io_err("creating object dir", &object_dir, e))?;

        for (index, bytes) in shards.iter().enumerate() {
            let p = shard_path(&self.root, &manifest.object_id, index as u16);
            self.atomic_write(&p, bytes)?;
        }

        let mp = manifest_path(&self.root, &manifest.object_id);
        self.atomic_write(&mp, manifest.to_json_string().as_bytes())?;

        // Defensive re-read: refuse to report success if the committed
        // manifest cannot be verified back from disk.
        let reread = self.get_manifest(&manifest.object_id)?;
        if reread.manifest_digest_hex != manifest.manifest_digest_hex {
            return Err(EcError::Store(format!(
                "post-commit manifest verification failed for object {}",
                manifest.object_id
            )));
        }
        Ok(())
    }

    fn get_manifest(&self, object_id: &str) -> Result<Manifest, EcError> {
        ensure_safe_id(object_id)?;
        let p = manifest_path(&self.root, object_id);
        let json = match fs::read_to_string(&p) {
            Ok(j) => j,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Err(EcError::Store(format!(
                    "NOT_FOUND: object {object_id} (looked at {})",
                    p.display()
                )));
            }
            Err(e) => return Err(io_err("reading manifest", &p, e)),
        };
        Manifest::from_json_verified(&json)
    }

    fn read_shard(&self, object_id: &str, index: u16) -> Result<ShardRead, EcError> {
        ensure_safe_id(object_id)?;
        let p = shard_path(&self.root, object_id, index);
        match fs::read(&p) {
            Ok(bytes) => Ok(ShardRead::Present(bytes)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(ShardRead::Missing),
            Err(e) => Err(io_err("reading shard", &p, e)),
        }
    }

    fn write_shard(&self, object_id: &str, index: u16, bytes: &[u8]) -> Result<(), EcError> {
        ensure_safe_id(object_id)?;
        let manifest = self.get_manifest(object_id)?;
        if index >= manifest.shard_count {
            return Err(EcError::InvalidShardIndex {
                index,
                total: manifest.shard_count,
            });
        }
        let p = shard_path(&self.root, object_id, index);
        self.atomic_write(&p, bytes)
    }

    fn list_objects(&self) -> Result<Vec<String>, EcError> {
        let mut ids = Vec::new();
        let entries = match fs::read_dir(&self.root) {
            Ok(e) => e,
            Err(e) => return Err(io_err("listing store root", &self.root, e)),
        };
        for entry in entries {
            let entry = entry.map_err(|e| io_err("reading dir entry", &self.root, e))?;
            if !entry.path().is_dir() {
                continue;
            }
            if let Some(name) = entry.file_name().to_str() {
                if manifest_path(&self.root, name).exists() {
                    ids.push(name.to_string());
                }
            }
        }
        ids.sort();
        Ok(ids)
    }

    fn root_display(&self) -> String {
        self.root.display().to_string()
    }
}
