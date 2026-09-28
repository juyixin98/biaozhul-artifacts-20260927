//! In-memory [`ObjectStore`] for tests and the service's own integration
//! tests. Mirrors the filesystem semantics exactly: missing shard returns
//! [`ShardRead::Missing`], committed objects require a verifiable manifest.

use std::collections::BTreeMap;
use std::sync::Mutex;

use ec_core::error::EcError;
use ec_format::Manifest;

use crate::{ensure_safe_id, ObjectStore, ShardRead};

#[derive(Default)]
struct Obj {
    manifest_json: String,
    shards: BTreeMap<u16, Vec<u8>>,
}

/// Thread-safe in-memory store.
#[derive(Default)]
pub struct MemoryStore {
    inner: Mutex<BTreeMap<String, Obj>>,
}

impl MemoryStore {
    pub fn new() -> Self {
        Self::default()
    }

    /// Test helper: delete one shard file to simulate a missing shard.
    pub fn test_remove_shard(&self, object_id: &str, index: u16) {
        let mut g = self.inner.lock().unwrap();
        if let Some(obj) = g.get_mut(object_id) {
            obj.shards.remove(&index);
        }
    }

    /// Test helper: corrupt one shard in place.
    pub fn test_corrupt_shard(&self, object_id: &str, index: u16) {
        let mut g = self.inner.lock().unwrap();
        if let Some(obj) = g.get_mut(object_id) {
            if let Some(bytes) = obj.shards.get_mut(&index) {
                if bytes.is_empty() {
                    bytes.push(0xFF);
                } else {
                    bytes[0] ^= 0x01;
                }
            }
        }
    }

    /// Test helper: tamper with the stored manifest JSON field.
    pub fn test_tamper_manifest(&self, object_id: &str, tamper: impl FnOnce(&mut String)) {
        let mut g = self.inner.lock().unwrap();
        if let Some(obj) = g.get_mut(object_id) {
            tamper(&mut obj.manifest_json);
        }
    }

    /// Test/diagnostic helper: raw persisted manifest JSON.
    pub fn test_manifest_json(&self, object_id: &str) -> Option<String> {
        self.inner
            .lock()
            .unwrap()
            .get(object_id)
            .map(|o| o.manifest_json.clone())
    }
}

impl ObjectStore for MemoryStore {
    fn put_object(&self, manifest: &Manifest, shards: &[Vec<u8>]) -> Result<(), EcError> {
        ensure_safe_id(&manifest.object_id)?;
        if shards.len() != manifest.shard_count as usize {
            return Err(EcError::SizeMismatch {
                detail: "put_object: shard count mismatch".into(),
            });
        }
        let mut g = self.inner.lock().unwrap();
        if g.contains_key(&manifest.object_id) {
            return Err(EcError::Store(format!(
                "ALREADY_EXISTS: object {}",
                manifest.object_id
            )));
        }
        let mut map = BTreeMap::new();
        for (i, bytes) in shards.iter().enumerate() {
            map.insert(i as u16, bytes.clone());
        }
        g.insert(
            manifest.object_id.clone(),
            Obj {
                manifest_json: manifest.to_json_string(),
                shards: map,
            },
        );
        Ok(())
    }

    fn get_manifest(&self, object_id: &str) -> Result<Manifest, EcError> {
        ensure_safe_id(object_id)?;
        let g = self.inner.lock().unwrap();
        let obj = g.get(object_id).ok_or_else(|| {
            EcError::Store(format!("NOT_FOUND: object {object_id} (memory store)"))
        })?;
        Manifest::from_json_verified(&obj.manifest_json)
    }

    fn read_shard(&self, object_id: &str, index: u16) -> Result<ShardRead, EcError> {
        ensure_safe_id(object_id)?;
        let g = self.inner.lock().unwrap();
        let obj = g.get(object_id).ok_or_else(|| {
            EcError::Store(format!("NOT_FOUND: object {object_id} (memory store)"))
        })?;
        match obj.shards.get(&index) {
            Some(b) => Ok(ShardRead::Present(b.clone())),
            None => Ok(ShardRead::Missing),
        }
    }

    fn write_shard(&self, object_id: &str, index: u16, bytes: &[u8]) -> Result<(), EcError> {
        ensure_safe_id(object_id)?;
        let mut g = self.inner.lock().unwrap();
        let obj = g
            .get_mut(object_id)
            .ok_or_else(|| EcError::Store(format!("NOT_FOUND: object {object_id}")))?;
        let manifest = Manifest::from_json_verified(&obj.manifest_json)?;
        if index >= manifest.shard_count {
            return Err(EcError::InvalidShardIndex {
                index,
                total: manifest.shard_count,
            });
        }
        obj.shards.insert(index, bytes.to_vec());
        Ok(())
    }

    fn list_objects(&self) -> Result<Vec<String>, EcError> {
        Ok(self.inner.lock().unwrap().keys().cloned().collect())
    }

    fn root_display(&self) -> String {
        "memory-store".into()
    }
}
