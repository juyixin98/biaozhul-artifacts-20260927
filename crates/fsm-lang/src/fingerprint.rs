//! Canonical specification fingerprint.
//!
//! The fingerprint is SHA-256 over a canonical JSON encoding of the
//! specification: object keys sorted lexicographically, no insignificant
//! whitespace. Two specs that are structurally equal get the same
//! fingerprint regardless of key order or formatting.

use crate::model::Spec;
use sha2::{Digest, Sha256};

fn canonicalize(value: &serde_json::Value, out: &mut String) {
    use serde_json::Value::*;
    match value {
        Null => out.push_str("null"),
        Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Number(n) => out.push_str(&n.to_string()),
        String(s) => {
            // serde_json's escaping is deterministic; reuse it.
            let escaped = serde_json::to_string(s).expect("string serialization cannot fail");
            out.push_str(&escaped);
        }
        Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                canonicalize(item, out);
            }
            out.push(']');
        }
        Object(map) => {
            let mut keys: Vec<&str> = map.keys().map(|k| k.as_str()).collect();
            keys.sort();
            out.push('{');
            for (i, key) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                canonicalize(&serde_json::Value::String((*key).to_string()), out);
                out.push(':');
                canonicalize(&map[*key], out);
            }
            out.push('}');
        }
    }
}

/// Returns `(hex fingerprint, canonical JSON)`.
pub fn fingerprint(spec: &Spec) -> Result<(String, String), serde_json::Error> {
    let raw = serde_json::to_value(spec)?;
    let mut canonical = String::new();
    canonicalize(&raw, &mut canonical);
    let mut hasher = Sha256::new();
    hasher.update(canonical.as_bytes());
    let digest = hasher.finalize();
    Ok((hex::encode(digest), canonical))
}
