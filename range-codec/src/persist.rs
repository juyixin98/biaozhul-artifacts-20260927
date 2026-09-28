//! Filesystem persistence adapter.
//!
//! Artifacts live under a single data directory as `<id>.rc01` (container
//! bytes) plus `<id>.json` (metadata sidecar). Writes are atomic: bytes land
//! in a temporary file, are fsynced, then renamed over the target. Artifact
//! ids are validated against a strict charset, so ids can never escape the
//! data directory (no path traversal).

use crate::container::{decode, DecodeBudget, Decoded, ModelMode};
use crate::error::{CodecError, Result};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};

/// Metadata recorded next to an encoded artifact.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ArtifactMeta {
    pub id: String,
    pub mode: String,
    pub num_symbols: u16,
    pub declared_len: u64,
    pub chunks: u32,
    pub encoded_bytes: u64,
}

/// Artifact as stored on disk: validated container bytes plus metadata.
#[derive(Debug, Clone)]
pub struct Artifact {
    pub meta: ArtifactMeta,
    pub data: Vec<u8>,
}

/// Filesystem-backed artifact store.
#[derive(Debug, Clone)]
pub struct Store {
    root: PathBuf,
}

impl Store {
    pub fn new(root: impl AsRef<Path>) -> Result<Self> {
        let root = root.as_ref().to_path_buf();
        fs::create_dir_all(&root).map_err(|e| CodecError::Io {
            context: format!("create data dir {}", root.display()),
            message: e.to_string(),
        })?;
        Ok(Store { root })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Ids are `[A-Za-z0-9_-]` with length 1..=64.
    fn validate_id(id: &str) -> Result<()> {
        if id.is_empty()
            || id.len() > 64
            || !id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
        {
            return Err(CodecError::BadRequest(format!(
                "invalid artifact id {id:?}: use 1..=64 chars of [A-Za-z0-9_-]"
            )));
        }
        Ok(())
    }

    fn data_path(&self, id: &str) -> PathBuf {
        self.root.join(format!("{id}.rc01"))
    }
    fn meta_path(&self, id: &str) -> PathBuf {
        self.root.join(format!("{id}.json"))
    }

    /// Persist container bytes atomically and write the sidecar. The bytes
    /// are validated (`decode`) before anything is written, so the store can
    /// never contain an artifact its own decoder rejects.
    pub fn put(&self, id: &str, mode: ModelMode, data: &[u8]) -> Result<ArtifactMeta> {
        Self::validate_id(id)?;
        let budget = DecodeBudget::default();
        let decoded: Decoded = decode(data, &budget)?;

        let meta = ArtifactMeta {
            id: id.to_string(),
            mode: match mode {
                ModelMode::Static => "static".into(),
                ModelMode::Adaptive => "adaptive".into(),
            },
            num_symbols: decoded.num_symbols,
            declared_len: decoded.symbols.len() as u64,
            chunks: decoded.chunks as u32,
            encoded_bytes: data.len() as u64,
        };

        atomic_write(&self.data_path(id), data)?;
        let sidecar = serde_json::to_vec_pretty(&meta).map_err(|e| CodecError::Io {
            context: "serialize metadata".into(),
            message: e.to_string(),
        })?;
        atomic_write(&self.meta_path(id), &sidecar)?;
        Ok(meta)
    }

    /// Load and re-validate an artifact.
    pub fn get(&self, id: &str) -> Result<Artifact> {
        Self::validate_id(id)?;
        let dp = self.data_path(id);
        let mp = self.meta_path(id);
        if !dp.exists() {
            return Err(CodecError::NotFound(id.to_string()));
        }
        let data = fs::read(&dp).map_err(|e| CodecError::Io {
            context: format!("read artifact {id}"),
            message: e.to_string(),
        })?;
        let meta: ArtifactMeta = if mp.exists() {
            let raw = fs::read(&mp).map_err(|e| CodecError::Io {
                context: format!("read metadata {id}"),
                message: e.to_string(),
            })?;
            serde_json::from_slice(&raw).map_err(|e| {
                CodecError::StoredArtifact(format!("metadata corrupt: {e}"))
            })?
        } else {
            return Err(CodecError::StoredArtifact(format!(
                "artifact {id} missing metadata sidecar"
            )));
        };

        // Never trust bytes loaded from disk: revalidate fully.
        let decoded = decode(&data, &DecodeBudget::default())?;
        if decoded.symbols.len() as u64 != meta.declared_len {
            return Err(CodecError::StoredArtifact(format!(
                "sidecar length {} disagrees with decoded {}",
                meta.declared_len,
                decoded.symbols.len()
            )));
        }
        Ok(Artifact { meta, data })
    }

    /// Decode an artifact directly.
    pub fn decode(&self, id: &str, budget: &DecodeBudget) -> Result<Decoded> {
        let art = self.get(id)?;
        decode(&art.data, budget)
    }

    /// Remove an artifact (idempotent-ish: missing file is an error).
    pub fn delete(&self, id: &str) -> Result<()> {
        Self::validate_id(id)?;
        if !self.data_path(id).exists() {
            return Err(CodecError::NotFound(id.to_string()));
        }
        fs::remove_file(self.data_path(id)).map_err(|e| CodecError::Io {
            context: format!("delete artifact {id}"),
            message: e.to_string(),
        })?;
        let _ = fs::remove_file(self.meta_path(id));
        Ok(())
    }
}

/// Write to `<target>.tmp-<pid>` then rename; fsync both file and directory.
fn atomic_write(target: &Path, bytes: &[u8]) -> Result<()> {
    let tmp = target.with_extra_extension("tmp");
    fs::write(&tmp, bytes).map_err(|e| CodecError::Io {
        context: format!("write {}", tmp.display()),
        message: e.to_string(),
    })?;
    // Best-effort durability; rename is the atomicity boundary.
    if let Ok(f) = fs::File::open(&tmp) {
        let _ = f.sync_all();
    }
    fs::rename(&tmp, target).map_err(|e| CodecError::Io {
        context: format!("rename to {}", target.display()),
        message: e.to_string(),
    })?;
    if let Some(parent) = target.parent() {
        if let Ok(f) = fs::File::open(parent) {
            let _ = f.sync_all();
        }
    }
    Ok(())
}

/// Small helper: `foo.rc01` -> `foo.rc01.tmp`.
trait WithExtraExt {
    fn with_extra_extension(&self, ext: &str) -> PathBuf;
}
impl WithExtraExt for Path {
    fn with_extra_extension(&self, ext: &str) -> PathBuf {
        let mut s = self.as_os_str().to_owned();
        s.push(".");
        s.push(ext);
        PathBuf::from(s)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::container::{encode_empty, encode_static, StaticChunk};

    fn temp_store() -> Store {
        let dir = std::env::temp_dir().join(format!(
            "range-codec-test-{}-{}",
            std::process::id(),
            uuid::Uuid::new_v4().simple()
        ));
        Store::new(&dir).unwrap()
    }

    #[test]
    fn put_get_decode_roundtrip() {
        let store = temp_store();
        let chunk = StaticChunk {
            freqs: vec![3, 1, 2],
            symbols: vec![0, 0, 2, 1, 0, 2],
        };
        let data = encode_static(&[chunk]).unwrap();
        let meta = store.put("demo-1", ModelMode::Static, &data).unwrap();
        assert_eq!(meta.declared_len, 6);
        assert_eq!(meta.chunks, 1);

        let art = store.get("demo-1").unwrap();
        assert_eq!(art.data, data);

        let d = store.decode("demo-1", &DecodeBudget::default()).unwrap();
        assert_eq!(d.symbols, vec![0, 0, 2, 1, 0, 2]);
    }

    #[test]
    fn empty_artifact_roundtrip() {
        let store = temp_store();
        let data = encode_empty(ModelMode::Adaptive, 4).unwrap();
        store.put("empty", ModelMode::Adaptive, &data).unwrap();
        let d = store.decode("empty", &DecodeBudget::default()).unwrap();
        assert!(d.symbols.is_empty());
    }

    #[test]
    fn rejects_invalid_and_path_traversal_ids() {
        let store = temp_store();
        for bad in ["", "../etc", "a/b", "..", "x x", &"x".repeat(65)] {
            assert!(
                matches!(store.get(bad), Err(CodecError::BadRequest(_))),
                "id {bad:?} should be rejected as bad request"
            );
        }
    }

    #[test]
    fn put_rejects_corrupt_container() {
        let store = temp_store();
        // Valid magic + version, then nothing else: truncated header.
        let err = store
            .put("bad", ModelMode::Static, &[0x52, 0x43, 0x30, 0x31, 1])
            .unwrap_err();
        assert!(matches!(err, CodecError::Truncated { .. }), "got {err:?}");
        assert!(store.get("bad").is_err());
    }

    #[test]
    fn missing_artifact_is_not_found() {
        let store = temp_store();
        assert!(matches!(
            store.get("nope"),
            Err(CodecError::NotFound(ref id)) if id == "nope"
        ));
    }
}
