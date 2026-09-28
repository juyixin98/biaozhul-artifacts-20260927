//! Fixed on-disk path layout helpers.

use std::path::{Path, PathBuf};

/// `<root>/<object_id>/manifest.json`
pub fn manifest_path(root: &Path, object_id: &str) -> PathBuf {
    root.join(object_id).join("manifest.json")
}

/// `<root>/<object_id>/shards`
pub fn shards_dir(root: &Path, object_id: &str) -> PathBuf {
    root.join(object_id).join("shards")
}

/// `<root>/<object_id>/shards/shard-NNNNN.bin` — fixed width 5 digits
/// (max index 254) keeps filesystem listing ordered and human-scannable.
pub fn shard_path(root: &Path, object_id: &str, index: u16) -> PathBuf {
    shards_dir(root, object_id).join(format!("shard-{index:05}.bin"))
}
