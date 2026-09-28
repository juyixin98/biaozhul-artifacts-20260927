//! Canonical JSON hashing used by the decision-log chain and snapshots.
//!
//! serde_json is built without the `preserve_order` feature, so its `Map` is
//! a `BTreeMap`: object keys are emitted in sorted order. Canonical form is
//! therefore just compact `serde_json` serialisation of the exact value that
//! gets hashed. `canonical_digest` strips any pre-existing `digest` field so
//! a structure can carry its own digest without feeding it back into the hash.

use serde_json::Value;
use sha2::{Digest, Sha256};

pub fn canonical_bytes(v: &Value) -> Vec<u8> {
    // serde_json::to_vec is deterministic given BTreeMap-backed objects.
    serde_json::to_vec(v).expect("Value is always serialisable")
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    hex::encode(h.finalize())
}

/// Digest over `v` after removing its own `digest` field.
pub fn canonical_digest(v: &Value) -> String {
    let mut v = v.clone();
    if let Some(obj) = v.as_object_mut() {
        obj.remove("digest");
    }
    sha256_hex(&canonical_bytes(&v))
}
