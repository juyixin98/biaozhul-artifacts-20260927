//! Filesystem persistence adapter.
//!
//! On-disk layout:
//!
//! ```text
//! <data_dir>/
//!   <object_id>/
//!       manifest.json
//!       shard-000.bin
//!       shard-001.bin
//!       ...
//! ```
//!
//! Writes are atomic: files go to `*.tmp` and are renamed into place, and a
//! new object is built in a temporary directory renamed on success, so a
//! crashed request never leaves a half-written object that looks valid.
//!
//! Object ids are restricted to a safe character set; path traversal via
//! `..` or `/` is rejected before touching the filesystem.

use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};
use tokio::fs;

use crate::error::{AppError, AppResult};
use crate::manifest::{Manifest, ShardRecord};

pub fn shard_file_name(index: u8) -> String {
    format!("shard-{index:03}.bin")
}

/// Validate an externally supplied object id.
pub fn validate_object_id(id: &str) -> AppResult<()> {
    if id.is_empty() || id.len() > 128 {
        return Err(AppError::bad_request(
            "INVALID_OBJECT_ID",
            "object id must be 1..128 chars",
        ));
    }
    let allowed = |c: char| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.');
    if !id.chars().all(allowed) {
        return Err(AppError::bad_request(
            "INVALID_OBJECT_ID",
            "object id may contain only [A-Za-z0-9._-]",
        ));
    }
    if id == "." || id == ".." {
        return Err(AppError::bad_request("INVALID_OBJECT_ID", "illegal object id"));
    }
    Ok(())
}

#[derive(Clone)]
pub struct FileStore {
    pub root: PathBuf,
}

/// Outcome of checking one shard position against disk and the manifest.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ShardStatus {
    /// File present and SHA-256 matches the manifest entry.
    Ok,
    /// No file at the expected path (an *erasure*).
    Missing,
    /// File present but its digest does not match the manifest (a *bad* shard).
    Corrupt { expected: String, computed: String },
    /// The manifest lists a different file name/ size than the fixed layout.
    ManifestMismatch(String),
}

impl ShardStatus {
    pub fn label(&self) -> &'static str {
        match self {
            ShardStatus::Ok => "ok",
            ShardStatus::Missing => "missing",
            ShardStatus::Corrupt { .. } => "corrupt",
            ShardStatus::ManifestMismatch(_) => "manifest_mismatch",
        }
    }
}

/// The complete verification picture for one object.
#[derive(Debug, Clone)]
pub struct ObjectAudit {
    pub ok_indices: Vec<u8>,
    pub missing_indices: Vec<u8>,
    pub corrupt_indices: Vec<u8>,
    pub manifest_mismatch_indices: Vec<u8>,
    pub per_shard: Vec<(u8, ShardStatus)>,
    /// Bytes of shards that are present AND verified (`index -> bytes`).
    pub verified_bytes: Vec<(u8, Vec<u8>)>,
}

impl ObjectAudit {
    /// Erasure count treated uniformly as unavailable: missing + corrupt
    /// (bad shards are handled exactly like erasures — never used).
    pub fn unavailable_count(&self) -> usize {
        self.missing_indices.len() + self.corrupt_indices.len()
    }
    pub fn recoverable(&self, k: u8) -> bool {
        self.ok_indices.len() >= k as usize
    }
}

impl FileStore {
    pub async fn new(root: impl Into<PathBuf>) -> AppResult<Self> {
        let root = root.into();
        fs::create_dir_all(&root).await?;
        Ok(Self { root })
    }

    pub fn object_dir(&self, object_id: &str) -> PathBuf {
        self.root.join(object_id)
    }

    pub fn manifest_path(&self, object_id: &str) -> PathBuf {
        self.object_dir(object_id).join("manifest.json")
    }

    /// List object directories under the root.
    pub async fn list_objects(&self) -> AppResult<Vec<String>> {
        let mut out = Vec::new();
        let mut rd = fs::read_dir(&self.root).await?;
        while let Some(entry) = rd.next_entry().await? {
            if entry.path().is_dir() {
                if let Some(name) = entry.file_name().to_str() {
                    out.push(name.to_string());
                }
            }
        }
        out.sort();
        Ok(out)
    }

    /// Read and return the verified manifest, or a NotFound error.
    pub async fn read_manifest(&self, object_id: &str) -> AppResult<Manifest> {
        let path = self.manifest_path(object_id);
        match fs::read(&path).await {
            Ok(bytes) => Ok(crate::manifest::parse_and_verify(&bytes)?),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                Err(AppError::not_found(format!("no manifest for object {object_id}")))
            }
            Err(e) => Err(e.into()),
        }
    }

    pub async fn object_exists(&self, object_id: &str) -> bool {
        self.manifest_path(object_id).exists()
    }

    /// Persist a fully formed object (manifest + shard bytes) atomically.
    pub async fn write_object(
        &self,
        object_id: &str,
        manifest: &Manifest,
        shards: &[Vec<u8>],
    ) -> AppResult<()> {
        validate_object_id(object_id)?;
        if self.object_exists(object_id).await {
            return Err(AppError::conflict(
                "OBJECT_ALREADY_EXISTS",
                format!("object {object_id} already exists"),
            ));
        }
        let final_dir = self.object_dir(object_id);
        let tmp_dir = self.root.join(format!(".tmp-{object_id}-{}", std::process::id()));
        // Clean any stale temp dir from an earlier crashed attempt.
        let _ = fs::remove_dir_all(&tmp_dir).await;
        fs::create_dir_all(&tmp_dir).await?;

        let write_result: AppResult<()> = async {
            // Shard files first.
            for rec in &manifest.shards {
                let bytes = shards
                    .get(rec.index as usize)
                    .ok_or_else(|| AppError::internal("shard missing from encode output"))?;
                atomic_write(&tmp_dir.join(&rec.file), bytes).await?;
            }
            // Manifest last among data files.
            let manifest_bytes = serde_json::to_vec_pretty(manifest)
                .map_err(|e| AppError::internal(format!("manifest serialize: {e}")))?;
            atomic_write(&tmp_dir.join("manifest.json"), &manifest_bytes).await?;
            Ok(())
        }
        .await;

        if let Err(e) = write_result {
            let _ = fs::remove_dir_all(&tmp_dir).await;
            return Err(e);
        }
        fs::rename(&tmp_dir, &final_dir).await?;
        Ok(())
    }

    /// Inspect every shard listed in the manifest, categorizing it as OK,
    /// missing, or corrupt. Verified shard bytes are returned for decoding.
    pub async fn audit(&self, manifest: &Manifest) -> AppResult<ObjectAudit> {
        let dir = self.object_dir(&manifest.object_id);
        let mut ok = Vec::new();
        let mut missing = Vec::new();
        let mut corrupt = Vec::new();
        let mut mismatch = Vec::new();
        let mut per_shard = Vec::new();
        let mut verified_bytes = Vec::new();

        for rec in &manifest.shards {
            let expected_file = shard_file_name(rec.index);
            if rec.file != expected_file {
                mismatch.push(rec.index);
                per_shard.push((
                    rec.index,
                    ShardStatus::ManifestMismatch(format!(
                        "manifest lists {}, layout expects {expected_file}",
                        rec.file
                    )),
                ));
                continue;
            }
            let path = dir.join(&rec.file);
            let bytes = match fs::read(&path).await {
                Ok(b) => b,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    missing.push(rec.index);
                    per_shard.push((rec.index, ShardStatus::Missing));
                    continue;
                }
                Err(e) => return Err(e.into()),
            };
            if bytes.len() as u64 != rec.size {
                corrupt.push(rec.index);
                per_shard.push((
                    rec.index,
                    ShardStatus::Corrupt {
                        expected: format!("{} bytes / {}", rec.size, rec.sha256),
                        computed: format!("{} bytes", bytes.len()),
                    },
                ));
                continue;
            }
            let hash = hex::encode(Sha256::digest(&bytes));
            if hash != rec.sha256 {
                corrupt.push(rec.index);
                per_shard.push((rec.index, ShardStatus::Corrupt {
                    expected: rec.sha256.clone(),
                    computed: hash,
                }));
                continue;
            }
            ok.push(rec.index);
            per_shard.push((rec.index, ShardStatus::Ok));
            verified_bytes.push((rec.index, bytes));
        }

        Ok(ObjectAudit {
            ok_indices: ok,
            missing_indices: missing,
            corrupt_indices: corrupt,
            manifest_mismatch_indices: mismatch,
            per_shard,
            verified_bytes,
        })
    }

    /// Rewrite the shards of an existing object from a reconstructed full
    /// set, and replace the manifest. Used by the repair operation; writes go
    /// through a sibling temp directory and an atomic swap.
    pub async fn replace_object_shards(
        &self,
        manifest: &Manifest,
        all_shards: &[Vec<u8>],
    ) -> AppResult<()> {
        let object_id = manifest.object_id.clone();
        let final_dir = self.object_dir(&object_id);
        let tmp_dir = self
            .root
            .join(format!(".repair-{object_id}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&tmp_dir).await;
        fs::create_dir_all(&tmp_dir).await?;

        for rec in &manifest.shards {
            let bytes = all_shards
                .get(rec.index as usize)
                .ok_or_else(|| AppError::internal("reconstruction missing shard"))?;
            atomic_write(&tmp_dir.join(&rec.file), bytes).await?;
        }
        let manifest_bytes = serde_json::to_vec_pretty(manifest)
            .map_err(|e| AppError::internal(format!("manifest serialize: {e}")))?;
        atomic_write(&tmp_dir.join("manifest.json"), &manifest_bytes).await?;

        // Swap: move old aside, new into place, remove old.
        let backup = self
            .root
            .join(format!(".old-{object_id}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&backup).await;
        fs::rename(&final_dir, &backup).await?;
        if let Err(e) = fs::rename(&tmp_dir, &final_dir).await {
            // roll back
            let _ = fs::rename(&backup, &final_dir).await;
            return Err(e.into());
        }
        let _ = fs::remove_dir_all(&backup).await;
        Ok(())
    }

    /// Delete one shard file (test/admin helper).
    pub async fn remove_shard_file(&self, object_id: &str, index: u8) -> AppResult<()> {
        let path: PathBuf = self
            .object_dir(object_id)
            .join(shard_file_name(index));
        match fs::remove_file(&path).await {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e.into()),
        }
    }
}

async fn atomic_write(path: &Path, bytes: &[u8]) -> AppResult<()> {
    let tmp = path.with_extension("bin.tmp");
    fs::write(&tmp, bytes).await?;
    fs::rename(&tmp, path).await?;
    Ok(())
}

/// Compute the SHA-256 hex digest of a shard record payload (helper used at
/// encode time so service code does not depend on sha2 directly).
pub fn sha256_hex(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

/// Build shard records from encoded shards, in index order.
pub fn records_for_shards(k: u8, shards: &[Vec<u8>]) -> Vec<ShardRecord> {
    shards
        .iter()
        .enumerate()
        .map(|(i, bytes)| ShardRecord {
            index: i as u8,
            role: if i < k as usize { "data".into() } else { "parity".into() },
            file: shard_file_name(i as u8),
            size: bytes.len() as u64,
            sha256: sha256_hex(bytes),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn id_validation_blocks_traversal() {
        assert!(validate_object_id("abc-123_X").is_ok());
        assert!(validate_object_id("../etc").is_err());
        assert!(validate_object_id("a/b").is_err());
        assert!(validate_object_id("").is_err());
        assert!(validate_object_id(&"x".repeat(129)).is_err());
    }
}
