//! Service orchestration: ties the coding kernel, manifest and store together.
//!
//! Security rule implemented here and nowhere bypassed:
//! - Shards whose SHA-256 does not match the authenticated manifest are
//!   **corrupt** and are treated identically to *missing* shards: as
//!   erasures. They are never fed to the decoder.
//! - Reconstruction needs at least `k` verified shards; with fewer, the
//!   service returns an explicit `NOT_RECOVERABLE` error and no payload bytes.

use std::time::SystemTime;

use sha2::{Digest, Sha256};
use tracing::{debug, info, warn};

use crate::erasure;
use crate::error::{AppError, AppResult};
use crate::manifest::{seal, AlgorithmSpec, Manifest};
use crate::storage::{records_for_shards, FileStore, ObjectAudit};

#[derive(Clone)]
pub struct AppState {
    pub store: FileStore,
    pub allowed_profiles: Vec<(u8, u8)>,
    pub max_object_bytes: usize,
}

#[derive(Debug, serde::Serialize)]
pub struct ShardReport {
    pub index: u8,
    pub role: String,
    pub status: String,
    /// Present only for corrupt shards.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expected_sha256: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub computed_sha256: Option<String>,
}

/// Result of an integrity inspection.
#[derive(Debug, serde::Serialize)]
pub struct InspectReport {
    pub object_id: String,
    pub k: u8,
    pub m: u8,
    pub original_len: u64,
    pub payload_sha256: String,
    pub status: String, // "intact" | "degraded_recoverable" | "not_recoverable"
    pub ok_shards: Vec<u8>,
    pub missing_shards: Vec<u8>,
    pub corrupt_shards: Vec<u8>,
    pub manifest_mismatch_shards: Vec<u8>,
    pub shards: Vec<ShardReport>,
    /// True when the verified shards alone suffice for reconstruction.
    pub recoverable: bool,
    /// Tolerance margin: verified shards minus k.
    pub margin: i64,
}

/// Result of a repair.
#[derive(Debug, serde::Serialize)]
pub struct RepairReport {
    pub object_id: String,
    pub rebuilt_shards: Vec<u8>,
    pub status_before: String,
    pub status_after: String,
    pub post_repair_verified: bool,
}

fn now_unix() -> u64 {
    SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

impl AppState {
    fn require_profile(&self, k: u8, m: u8) -> AppResult<()> {
        erasure::validate_params(k, m)?;
        if !self.allowed_profiles.contains(&(k, m)) {
            return Err(AppError::bad_request(
                "PROFILE_NOT_ALLOWED",
                format!(
                    "(k={k},m={m}) is not in the allowed list {:?}",
                    self.allowed_profiles
                ),
            ));
        }
        Ok(())
    }

    /// Encode and persist a new object.
    pub async fn put_object(
        &self,
        object_id: &str,
        data: &[u8],
        k: u8,
        m: u8,
    ) -> AppResult<Manifest> {
        self.require_profile(k, m)?;
        if data.len() > self.max_object_bytes {
            return Err(AppError::payload_too_large(format!(
                "payload {} bytes exceeds limit {}",
                data.len(),
                self.max_object_bytes
            )));
        }
        info!(
            object_id = %object_id, k = k, m = m, bytes = data.len(),
            "encode: zero-pad and build systematic Vandermonde shards"
        );

        // Encode on the blocking pool; the kernel is CPU bound.
        let data_owned = data.to_vec();
        let encoded = tokio::task::spawn_blocking(move || erasure::encode(&data_owned, k, m))
            .await
            .map_err(|e| AppError::internal(format!("encode task panicked: {e}")))??;

        let payload_sha256 = hex::encode(Sha256::digest(data));
        let original_len = data.len() as u64;
        let shard_len = encoded.shard_len as u64;
        let pad_len = (k as u64) * shard_len - original_len;
        let shards = encoded.shards;
        let records = records_for_shards(k, &shards);

        let mut manifest = Manifest {
            object_id: object_id.to_string(),
            algorithm: AlgorithmSpec::current(),
            k,
            m,
            original_len,
            shard_len,
            pad_len,
            created_at_unix: now_unix(),
            payload_sha256: payload_sha256.clone(),
            shards: records,
            manifest_digest: String::new(),
        };
        seal(&mut manifest);

        self.store
            .write_object(object_id, &manifest, &shards)
            .await?;
        info!(
            object_id = %object_id, shard_len = shard_len, pad_len = pad_len,
            payload_sha256 = %payload_sha256,
            "encode: object persisted (manifest sealed)"
        );
        Ok(manifest)
    }

    /// Load manifest and audit all shards.
    async fn load_and_audit(&self, object_id: &str) -> AppResult<(Manifest, ObjectAudit)> {
        let manifest = self.store.read_manifest(object_id).await?;
        let audit = self.store.audit(&manifest).await?;
        Ok((manifest, audit))
    }

    fn status_label(k: u8, audit: &ObjectAudit) -> String {
        if audit.unavailable_count() == 0 && audit.manifest_mismatch_indices.is_empty() {
            "intact".to_string()
        } else if audit.recoverable(k) {
            "degraded_recoverable".to_string()
        } else {
            "not_recoverable".to_string()
        }
    }

    /// Inspect integrity without changing anything.
    pub async fn inspect(&self, object_id: &str) -> AppResult<InspectReport> {
        let (manifest, audit) = self.load_and_audit(object_id).await?;
        let status = Self::status_label(manifest.k, &audit);
        if status == "not_recoverable" {
            warn!(
                object_id = %object_id,
                ok = audit.ok_indices.len(),
                missing = ?audit.missing_indices,
                corrupt = ?audit.corrupt_indices,
                need = manifest.k,
                "inspect: NOT recoverable — fewer than k verified shards"
            );
        } else if status != "intact" {
            warn!(
                object_id = %object_id,
                missing = ?audit.missing_indices,
                corrupt = ?audit.corrupt_indices,
                "inspect: degraded but recoverable"
            );
        } else {
            debug!(object_id = %object_id, "inspect: intact");
        }

        let shards = audit
            .per_shard
            .iter()
            .map(|(idx, st)| {
                let role = if (*idx as usize) < manifest.k as usize {
                    "data"
                } else {
                    "parity"
                };
                let (expected, computed) = match st {
                    crate::storage::ShardStatus::Corrupt { expected, computed } => {
                        (Some(expected.clone()), Some(computed.clone()))
                    }
                    _ => (None, None),
                };
                ShardReport {
                    index: *idx,
                    role: role.to_string(),
                    status: st.label().to_string(),
                    expected_sha256: expected,
                    computed_sha256: computed,
                }
            })
            .collect();

        Ok(InspectReport {
            object_id: object_id.to_string(),
            k: manifest.k,
            m: manifest.m,
            original_len: manifest.original_len,
            payload_sha256: manifest.payload_sha256.clone(),
            status,
            ok_shards: audit.ok_indices.clone(),
            missing_shards: audit.missing_indices.clone(),
            corrupt_shards: audit.corrupt_indices.clone(),
            manifest_mismatch_shards: audit.manifest_mismatch_indices.clone(),
            shards,
            recoverable: audit.recoverable(manifest.k),
            margin: audit.ok_indices.len() as i64 - manifest.k as i64,
        })
    }

    /// Reconstruct and return the *original* payload. Refuses unless at least
    /// `k` shards passed integrity verification; truncates padding according
    /// to the authenticated original length and cross-checks the payload
    /// digest before returning.
    pub async fn get_object(&self, object_id: &str) -> AppResult<Vec<u8>> {
        let (manifest, audit) = self.load_and_audit(object_id).await?;

        if !audit.recoverable(manifest.k) {
            return Err(AppError::new(
                axum::http::StatusCode::CONFLICT,
                "NOT_RECOVERABLE",
                format!(
                    "only {} shards verified; {} required; refusing to return unverifiable data",
                    audit.ok_indices.len(),
                    manifest.k
                ),
            )
            .with_detail(serde_json::json!({
                "verified": audit.ok_indices,
                "missing": audit.missing_indices,
                "corrupt": audit.corrupt_indices,
                "need": manifest.k,
            })));
        }

        let k = manifest.k;
        let m = manifest.m;
        let shard_len = manifest.shard_len as usize;
        let available = audit.verified_bytes.clone();
        let used_count = available.len();
        let full = tokio::task::spawn_blocking(move || {
            erasure::reconstruct(&available, k, m, shard_len)
        })
        .await
        .map_err(|e| AppError::internal(format!("reconstruct task panicked: {e}")))??;

        // Concatenate data shards and truncate to the authenticated length.
        let kk = manifest.k as usize;
        let mut out = Vec::with_capacity((manifest.shard_len as usize) * kk);
        for shard in full.iter().take(kk) {
            out.extend_from_slice(shard);
        }
        if out.len() < manifest.original_len as usize {
            return Err(AppError::internal(
                "reconstructed data shorter than authenticated original_len",
            ));
        }
        out.truncate(manifest.original_len as usize);

        // Final cross-check: payload digest must match the manifest.
        let computed = hex::encode(Sha256::digest(&out));
        if computed != manifest.payload_sha256 {
            return Err(AppError::new(
                axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                "RECONSTRUCTED_PAYLOAD_DIGEST_MISMATCH",
                "reconstruction produced bytes whose digest differs from the manifest",
            )
            .with_detail(serde_json::json!({
                "expected": manifest.payload_sha256,
                "computed": computed,
            })));
        }
        info!(
            object_id = %object_id,
            used_shards = used_count,
            missing = ?audit.missing_indices,
            corrupt = ?audit.corrupt_indices,
            "reconstruct: payload verified against manifest digest"
        );
        Ok(out)
    }

    /// Rebuild missing/corrupt shards on disk. No-op (but reported) when
    /// intact. Refuses with NOT_RECOVERABLE below k verified shards.
    pub async fn repair(&self, object_id: &str) -> AppResult<RepairReport> {
        let (manifest, audit) = self.load_and_audit(object_id).await?;
        let status_before = Self::status_label(manifest.k, &audit);

        if status_before == "intact" {
            return Ok(RepairReport {
                object_id: object_id.to_string(),
                rebuilt_shards: vec![],
                status_before,
                status_after: "intact".to_string(),
                post_repair_verified: true,
            });
        }
        if !audit.recoverable(manifest.k) {
            return Err(AppError::new(
                axum::http::StatusCode::CONFLICT,
                "NOT_RECOVERABLE",
                format!(
                    "{} verified shards, need {}; repair impossible without fabricating data",
                    audit.ok_indices.len(),
                    manifest.k
                ),
            )
            .with_detail(serde_json::json!({
                "verified": audit.ok_indices,
                "missing": audit.missing_indices,
                "corrupt": audit.corrupt_indices,
            })));
        }

        let need_rebuild: Vec<u8> = audit
            .missing_indices
            .iter()
            .chain(audit.corrupt_indices.iter())
            .chain(audit.manifest_mismatch_indices.iter())
            .copied()
            .collect();
        info!(
            object_id = %object_id,
            rebuilding = ?need_rebuild,
            from_verified = audit.ok_indices.len(),
            "repair: reconstructing full shard set"
        );

        let k = manifest.k;
        let m = manifest.m;
        let shard_len = manifest.shard_len as usize;
        let available = audit.verified_bytes.clone();
        let full = tokio::task::spawn_blocking(move || {
            erasure::reconstruct(&available, k, m, shard_len)
        })
        .await
        .map_err(|e| AppError::internal(format!("reconstruct task panicked: {e}")))??;

        // Persist the rebuilt set, then re-audit from disk to prove the
        // repair actually held (never trust in-memory success alone).
        self.store
            .replace_object_shards(&manifest, &full)
            .await?;
        let post_manifest = self.store.read_manifest(object_id).await?;
        let post_audit = self.store.audit(&post_manifest).await?;
        let post_ok = post_audit.unavailable_count() == 0
            && post_audit.manifest_mismatch_indices.is_empty();
        let status_after = if post_ok {
            "intact".to_string()
        } else {
            Self::status_label(post_manifest.k, &post_audit)
        };
        info!(
            object_id = %object_id,
            post_status = %status_after,
            "repair: completed and re-verified from disk"
        );
        Ok(RepairReport {
            object_id: object_id.to_string(),
            rebuilt_shards: need_rebuild,
            status_before,
            status_after,
            post_repair_verified: post_ok,
        })
    }
}
