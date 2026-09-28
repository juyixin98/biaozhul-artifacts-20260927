//! Application service: the place where kernel, format and store meet.
//!
//! It is deliberately synchronous and blocking (`std::fs` underneath); the
//! HTTP layer calls it inside `spawn_blocking`. All operations are
//! idempotent reads apart from explicit `encode` and `repair` persistence.

use std::sync::Arc;

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use ec_core::error::EcError;
use ec_core::reed_solomon::{
    encode as rs_encode, rebuild_shards, reconstruct_data, truncate_to_original,
};
use ec_core::{digest_shard, verify_shard, CodecConfig, Shard};
use ec_format::Manifest;
use ec_store::{ObjectStore, ShardRead};

use crate::report::{
    DecodeReport, EncodeReport, RepairReport, ShardStatus, VerifyReport,
};

/// Stateless shard input supplied directly by the caller (no store).
#[derive(Debug, Clone)]
pub struct InputShard {
    pub index: u16,
    pub bytes: Vec<u8>,
}

/// Either a store-backed service or one-off stateless verification: this
/// enum keeps the classification path identical for both.
#[derive(Clone)]
pub struct ErasureCodingService {
    store: Option<Arc<dyn ObjectStore>>,
}

/// Outcome of reading+classifying one shard set against a verified manifest.
pub struct Classification {
    pub report: VerifyReport,
    pub cfg: CodecConfig,
    pub good: Vec<Shard>,
}

impl ErasureCodingService {
    /// Store-backed service (filesystem or in-memory).
    pub fn new(store: Arc<dyn ObjectStore>) -> Self {
        Self { store: Some(store) }
    }

    /// No persistence: used by the stateless `/v1/decode` endpoint.
    pub fn stateless() -> Self {
        Self { store: None }
    }

    fn location(&self) -> String {
        self.store
            .as_ref()
            .map(|s| s.root_display())
            .unwrap_or_else(|| "stateless request".to_string())
    }

    // ---------- encode ----------

    /// Encode bytes, build+seal the manifest and persist everything.
    pub fn encode_store(
        &self,
        request_id: &str,
        object_id: String,
        cfg: CodecConfig,
        data: &[u8],
    ) -> Result<EncodeReport, EcError> {
        let store = self
            .store
            .as_ref()
            .ok_or_else(|| EcError::Internal("encode requires a configured store".into()))?;
        let mut steps = Vec::new();

        steps.push(format!(
            "[{request_id}] split+pad input: {} byte(s) over k={} data shard(s)",
            data.len(),
            cfg.k()
        ));
        let enc = rs_encode(&cfg, data);
        steps.push(format!(
            "[{request_id}] applied {} fixed Cauchy parity row(s); shard_len={} byte(s)",
            cfg.m(),
            enc.shard_len
        ));

        let digests: Vec<(u16, Vec<u8>)> = enc
            .shards
            .iter()
            .enumerate()
            .map(|(i, bytes)| (i as u16, digest_shard(i as u16, bytes)))
            .collect();
        steps.push(format!(
            "[{request_id}] computed {n} position-aware SHA-256 shard digest(s)",
            n = enc.shards.len()
        ));

        let manifest =
            Manifest::create(object_id.clone(), &cfg, enc.shard_len, data.len() as u64, digests)?;
        steps.push(format!(
            "[{request_id}] sealed manifest format_version={} field={} digest={}…",
            manifest.format_version,
            manifest.field_primitive,
            &manifest.manifest_digest_hex[..12]
        ));

        store.put_object(&manifest, &enc.shards)?;
        steps.push(format!(
            "[{request_id}] committed {} shard(s)+manifest atomically to {}",
            enc.shards.len(),
            store.root_display()
        ));

        tracing::info!(request_id, object_id = %object_id, steps = steps.len(), "object encoded and committed");

        Ok(EncodeReport {
            request_id: request_id.to_string(),
            object_id,
            location: store.root_display(),
            k: cfg.k() as u16,
            m: cfg.m() as u16,
            n: cfg.n() as u16,
            shard_len: enc.shard_len as u32,
            original_len: data.len() as u64,
            pad_len: manifest.pad_len,
            field_primitive: manifest.field_primitive.clone(),
            manifest_digest_hex: manifest.manifest_digest_hex,
            steps,
        })
    }

    // ---------- verify / classify ----------

    /// Load a manifest from the store and verify it.
    fn load_verified_manifest(&self, object_id: &str) -> Result<Manifest, EcError> {
        let store = self.store.as_ref().ok_or_else(|| {
            EcError::Internal("store operation needs a configured store".into())
        })?;
        store.get_manifest(object_id)
    }

    /// Classify all shards against a verified manifest. When
    /// `provided_shards` is `None`, shards come from the store; otherwise the
    /// given inputs are used (stateless mode) and absent indices count as
    /// missing.
    pub fn classify(
        &self,
        object_id: &str,
        manifest: &Manifest,
        provided: Option<&[InputShard]>,
    ) -> Classification {
        let cfg = match manifest.config() {
            Ok(c) => c,
            Err(e) => return self.classification_failure(object_id, manifest, e),
        };
        let mut report = VerifyReport::new(object_id.to_string(), self.location());
        report.format_version = manifest.format_version;
        report.field_primitive = manifest.field_primitive.clone();
        report.digest_algorithm = manifest.digest_algorithm.clone();
        report.k = manifest.k;
        report.m = manifest.m;
        report.shard_len = manifest.shard_len;
        report.original_len = manifest.original_len;
        report.pad_len = manifest.pad_len;
        report.manifest_digest_ok = manifest.verify_digest();

        report.step(format!("manifest digest verified: {}", report.manifest_digest_ok));
        report.step(format!(
            "classifying {n} shard(s) against position-aware SHA-256 digests",
            n = cfg.n()
        ));

        // Index provided inputs for stateless mode; duplicate indices in the
        // request are rejected explicitly below.
        let mut provided_map: std::collections::BTreeMap<u16, Vec<u8>> =
            std::collections::BTreeMap::new();
        if let Some(shards) = provided {
            for inp in shards {
                provided_map.entry(inp.index).or_insert_with(|| inp.bytes.clone());
            }
        }

        let mut good = Vec::new();
        for idx in 0..cfg.n() {
            let index = idx as u16;
            let expected = match manifest.shard_digest(idx) {
                Some(d) => d,
                None => {
                    report.shards.insert(index, ShardStatus::ReadError);
                    report.read_error_indices.push(index);
                    report
                        .uncertainties
                        .push(format!("shard {index}: expected digest absent from manifest"));
                    continue;
                }
            };

            let read: Result<ShardRead, EcError> = match (self.store.as_ref(), provided) {
                (Some(store), None) => store.read_shard(object_id, index),
                (_, Some(_)) => Ok(match provided_map.get(&index) {
                    Some(b) => ShardRead::Present(b.clone()),
                    None => ShardRead::Missing,
                }),
                (None, None) => Ok(ShardRead::Missing),
            };

            let status = match read {
                Ok(ShardRead::Missing) => ShardStatus::Missing,
                Ok(ShardRead::Present(bytes)) => {
                    if bytes.len() != manifest.shard_len as usize {
                        report.uncertainties.push(format!(
                            "shard {index}: length {} != manifest shard_len {} — classified bad",
                            bytes.len(),
                            manifest.shard_len
                        ));
                        ShardStatus::BadDigest
                    } else if verify_shard(index, &bytes, &expected) {
                        good.push(Shard::new(index, bytes));
                        ShardStatus::Good
                    } else {
                        ShardStatus::BadDigest
                    }
                }
                Err(e) => {
                    report.uncertainties.push(format!(
                        "shard {index}: unreadable ({e}) — neither trusted nor counted as an erasure proof"
                    ));
                    ShardStatus::ReadError
                }
            };
            report.shards.insert(index, status.clone());
            match status {
                ShardStatus::Good => report.good_count += 1,
                ShardStatus::Missing => report.missing_indices.push(index),
                ShardStatus::BadDigest => report.bad_digest_indices.push(index),
                ShardStatus::ReadError => report.read_error_indices.push(index),
                ShardStatus::Rebuilt => {}
            }
        }

        let available = report.good_count;
        if available >= cfg.k() {
            report.recoverable = true;
            report.step(format!(
                "{available} good shard(s) >= k={} → recovery possible; missing+bad treated as erasures",
                cfg.k()
            ));
        } else {
            report.failure_code = Some(EcError::InsufficientShards {
                available,
                required: cfg.k(),
            }
            .code()
            .to_string());
            report.step(format!(
                "only {available} good shard(s) < k={} → refusal path: no data will be fabricated",
                cfg.k()
            ));
        }

        Classification {
            report,
            cfg,
            good,
        }
    }

    fn classification_failure(
        &self,
        object_id: &str,
        manifest: &Manifest,
        err: EcError,
    ) -> Classification {
        let mut report = VerifyReport::new(object_id.to_string(), self.location());
        report.format_version = manifest.format_version;
        report.field_primitive = manifest.field_primitive.clone();
        report.k = manifest.k;
        report.m = manifest.m;
        report.shard_len = manifest.shard_len;
        report.original_len = manifest.original_len;
        report.pad_len = manifest.pad_len;
        report.manifest_digest_ok = manifest.verify_digest();
        report.failure_code = Some(err.code().to_string());
        report.uncertainties.push(err.to_string());
        Classification {
            report,
            cfg: CodecConfig::new(1, 1).unwrap(),
            good: Vec::new(),
        }
    }

    /// Store-backed verify: no reconstruction, just the classified report.
    pub fn verify_store(&self, request_id: &str, object_id: &str) -> Result<VerifyReport, EcError> {
        let manifest = self.load_verified_manifest(object_id)?;
        let mut c = self.classify(object_id, &manifest, None);
        c.report
            .step(format!("[{request_id}] verify-only request: no reconstruction performed"));
        tracing::info!(
            request_id,
            object_id,
            good = c.report.good_count,
            missing = ?c.report.missing_indices,
            bad_digest = ?c.report.bad_digest_indices,
            recoverable = c.report.recoverable,
            "verify complete"
        );
        Ok(c.report)
    }

    // ---------- decode / recover ----------

    /// Full recovery path shared by store and stateless modes. `data=false`
    /// returns the classification+possibility result without original bytes.
    fn recover(
        &self,
        request_id: &str,
        object_id: &str,
        manifest: &Manifest,
        provided: Option<&[InputShard]>,
        include_data: bool,
    ) -> DecodeReport {
        let mut steps = Vec::new();
        let mut failures = Vec::new();
        let uncertainties = Vec::new();

        // Explicit duplicate-index rejection (stateless inputs): this must
        // fail before any shard is used.
        if let Some(inputs) = provided {
            let mut seen = std::collections::BTreeSet::new();
            for inp in inputs {
                if !seen.insert(inp.index) {
                    let err = EcError::DuplicateShardIndex(inp.index);
                    failures.push(err.to_string());
                    let mut report =
                        VerifyReport::new(object_id.to_string(), self.location());
                    report.failure_code = Some(err.code().to_string());
                    report.step(format!("[{request_id}] rejected duplicate shard index {}", inp.index));
                    return DecodeReport {
                        request_id: request_id.to_string(),
                        object_id: object_id.to_string(),
                        location: self.location(),
                        recovered: false,
                        data_b64: None,
                        original_len: manifest.original_len,
                        verify: report,
                        used_shard_indices: vec![],
                        rebuilt_shard_indices: vec![],
                        failure_code: Some(err.code().to_string()),
                        failures,
                        uncertainties,
                        steps,
                    };
                }
            }
        }

        let Classification {
            mut report,
            cfg,
            good,
        } = self.classify(object_id, manifest, provided);

        if report.good_count < cfg.k() {
            let err = EcError::InsufficientShards {
                available: report.good_count,
                required: cfg.k(),
            };
            failures.push(err.to_string());
            report.failure_code = Some(err.code().to_string());
            report.step(format!("[{request_id}] recovery refused ({})", err.code()));
            tracing::warn!(
                request_id,
                object_id,
                good = report.good_count,
                required = cfg.k(),
                missing = ?report.missing_indices,
                bad = ?report.bad_digest_indices,
                "recovery refused: insufficient good shards"
            );
            return DecodeReport {
                request_id: request_id.to_string(),
                object_id: object_id.to_string(),
                location: self.location(),
                recovered: false,
                data_b64: None,
                original_len: manifest.original_len,
                verify: report,
                used_shard_indices: vec![],
                rebuilt_shard_indices: vec![],
                failure_code: Some(err.code().to_string()),
                failures,
                uncertainties,
                steps,
            };
        }

        steps.push(format!(
            "[{request_id}] solving coding equations from {} verified shard(s)",
            good.len()
        ));
        let recon = match reconstruct_data(&cfg, good, manifest.shard_len as usize) {
            Ok(r) => r,
            Err(e) => {
                failures.push(e.to_string());
                report.failure_code = Some(e.code().to_string());
                return DecodeReport {
                    request_id: request_id.to_string(),
                    object_id: object_id.to_string(),
                    location: self.location(),
                    recovered: false,
                    data_b64: None,
                    original_len: manifest.original_len,
                    verify: report,
                    used_shard_indices: vec![],
                    rebuilt_shard_indices: vec![],
                    failure_code: Some(e.code().to_string()),
                    failures,
                    uncertainties,
                    steps,
                };
            }
        };
        let used: Vec<u16> = recon.used_indices.iter().map(|i| *i as u16).collect();
        steps.push(format!(
            "[{request_id}] Gauss-Jordan solve succeeded using shard indices {used:?}"
        ));

        steps.push(format!(
            "[{request_id}] truncating {} padded byte(s) using authenticated original_len={}",
            manifest.pad_len, manifest.original_len
        ));
        let original = match truncate_to_original(
            &cfg,
            &recon.data_shards,
            manifest.shard_len as usize,
            manifest.original_len,
        ) {
            Ok(o) => o,
            Err(e) => {
                failures.push(e.to_string());
                report.failure_code = Some(e.code().to_string());
                return DecodeReport {
                    request_id: request_id.to_string(),
                    object_id: object_id.to_string(),
                    location: self.location(),
                    recovered: false,
                    data_b64: None,
                    original_len: manifest.original_len,
                    verify: report,
                    used_shard_indices: used,
                    rebuilt_shard_indices: vec![],
                    failure_code: Some(e.code().to_string()),
                    failures,
                    uncertainties,
                    steps,
                };
            }
        };

        // Final defensive check: byte length equals the authenticated length.
        assert_eq!(original.len() as u64, manifest.original_len);

        report.step(format!(
            "[{request_id}] recovered {} original byte(s); {} missing + {} digest-bad shard(s) handled as erasures",
            original.len(),
            report.missing_indices.len(),
            report.bad_digest_indices.len()
        ));
        tracing::info!(
            request_id,
            object_id,
            used = ?used,
            missing = ?report.missing_indices,
            bad_digest = ?report.bad_digest_indices,
            "object recovered"
        );

        let data_b64 = if include_data {
            Some(B64.encode(&original))
        } else {
            None
        };

        DecodeReport {
            request_id: request_id.to_string(),
            object_id: object_id.to_string(),
            location: self.location(),
            recovered: true,
            data_b64,
            original_len: manifest.original_len,
            verify: report,
            used_shard_indices: used,
            rebuilt_shard_indices: vec![],
            failure_code: None,
            failures,
            uncertainties,
            steps,
        }
    }

    /// Store-backed decode.
    pub fn decode_store(
        &self,
        request_id: &str,
        object_id: &str,
        include_data: bool,
    ) -> Result<DecodeReport, EcError> {
        let manifest = self.load_verified_manifest(object_id)?;
        Ok(self.recover(request_id, object_id, &manifest, None, include_data))
    }

    /// Stateless decode: caller supplies a verified-parseable manifest and the
    /// shards it has. Manifest digest is still verified here — a tampered
    /// manifest is rejected before classification.
    pub fn decode_stateless(
        &self,
        request_id: &str,
        manifest_json: &str,
        shards: Vec<InputShard>,
        include_data: bool,
    ) -> Result<DecodeReport, EcError> {
        let manifest = Manifest::from_json_str(manifest_json)?;
        let object_id = manifest.object_id.clone();
        if !manifest.verify_digest() {
            return Err(EcError::ManifestDigestMismatch);
        }
        Ok(self.recover(
            request_id,
            &object_id,
            &manifest,
            Some(&shards),
            include_data,
        ))
    }

    // ---------- repair ----------

    /// Rebuild requested shards from verified available shards. In store mode
    /// the rebuilt bytes are persisted (atomic writes); in stateless mode they
    /// are only returned.
    #[allow(clippy::too_many_arguments)]
    pub fn repair(
        &self,
        request_id: &str,
        object_id: &str,
        targets: Vec<u16>,
        provided: Option<&[InputShard]>,
        persist: bool,
        manifest_override: Option<&Manifest>,
    ) -> Result<RepairReport, EcError> {
        let mut steps = Vec::new();
        let mut failures = Vec::new();
        let uncertainties: Vec<String> = Vec::new();

        let owned_manifest;
        let manifest: &Manifest = match manifest_override {
            Some(m) => m,
            None => {
                owned_manifest = self.load_verified_manifest(object_id)?;
                &owned_manifest
            }
        };

        // De-duplicate + range-check targets up front.
        let mut unique_targets: Vec<usize> = Vec::new();
        for t in &targets {
            manifest.config()?.check_index(*t)?;
            let t = *t as usize;
            if !unique_targets.contains(&t) {
                unique_targets.push(t);
            }
        }
        unique_targets.sort_unstable();

        let Classification {
            mut report,
            cfg,
            good,
        } = self.classify(&manifest.object_id, manifest, provided);

        // Empty target list means "repair every shard currently missing,
        // digest-bad or unreadable" — the common recovery scenario. Explicit
        // targets were validated against layout above.
        let unique_targets: Vec<usize> = if unique_targets.is_empty() {
            let mut auto: Vec<usize> = report
                .shards
                .iter()
                .filter(|(_, st)| {
                    matches!(
                        st,
                        ShardStatus::Missing | ShardStatus::BadDigest | ShardStatus::ReadError
                    )
                })
                .map(|(idx, _)| *idx as usize)
                .collect();
            auto.sort_unstable();
            auto
        } else {
            unique_targets
        };

        if report.good_count < cfg.k() {
            let err = EcError::InsufficientShards {
                available: report.good_count,
                required: cfg.k(),
            };
            failures.push(err.to_string());
            report.failure_code = Some(err.code().to_string());
            return Ok(RepairReport {
                request_id: request_id.to_string(),
                object_id: object_id.to_string(),
                location: self.location(),
                repaired: false,
                targets,
                rebuilt_shards_b64: Default::default(),
                verify: report,
                used_shard_indices: vec![],
                failure_code: Some(err.code().to_string()),
                failures,
                uncertainties,
                steps,
            });
        }

        // Only targets currently missing/bad need rebuilding; a target that is
        // already good is simply returned from store bytes (no overwrite).
        let needed: Vec<usize> = unique_targets
            .iter()
            .copied()
            .filter(|t| {
                matches!(
                    report.shards.get(&(*t as u16)),
                    Some(ShardStatus::Missing) | Some(ShardStatus::BadDigest) | Some(ShardStatus::ReadError)
                )
            })
            .collect();

        steps.push(format!(
            "[{request_id}] rebuilding target shard(s) {needed:?} from {} good shard(s)",
            good.len()
        ));
        let (recon, rebuilt) = rebuild_shards(
            &cfg,
            good,
            manifest.shard_len as usize,
            &needed,
        )?;
        let used: Vec<u16> = recon.used_indices.iter().map(|i| *i as u16).collect();

        // Every rebuilt shard must satisfy its own manifest digest before we
        // expose/persist it — a self-verification gate.
        let mut rebuilt_b64 = std::collections::BTreeMap::new();
        for shard in &rebuilt {
            let expected = manifest
                .shard_digest(shard.index as usize)
                .ok_or_else(|| EcError::Internal("rebuilt shard digest missing".into()))?;
            if !verify_shard(shard.index, &shard.data, &expected) {
                return Err(EcError::NotReconstructable(format!(
                    "rebuilt shard {} failed its own manifest digest; refusing to persist",
                    shard.index
                )));
            }
            rebuilt_b64.insert(shard.index, B64.encode(&shard.data));
            report
                .shards
                .insert(shard.index, ShardStatus::Rebuilt);
            report.step(format!(
                "[{request_id}] rebuilt shard {} and verified it against manifest digest",
                shard.index
            ));
        }

        if persist {
            let store = self.store.as_ref().ok_or_else(|| {
                EcError::Internal("persistent repair requires a configured store".into())
            })?;
            for shard in &rebuilt {
                store.write_shard(object_id, shard.index, &shard.data)?;
            }
            steps.push(format!(
                "[{request_id}] persisted {} rebuilt shard(s) atomically",
                rebuilt.len()
            ));
        }

        tracing::info!(request_id, object_id, ?used, rebuilt = ?needed, "repair complete");
        Ok(RepairReport {
            request_id: request_id.to_string(),
            object_id: object_id.to_string(),
            location: self.location(),
            repaired: true,
            targets,
            rebuilt_shards_b64: rebuilt_b64,
            verify: report,
            used_shard_indices: used,
            failure_code: None,
            failures,
            uncertainties,
            steps,
        })
    }
}

// Re-export for DTO decoding.
pub fn b64_decode(input: &str) -> Result<Vec<u8>, EcError> {
    B64.decode(input.trim())
        .map_err(|e| EcError::Internal(format!("invalid base64 input: {e}")))
}
