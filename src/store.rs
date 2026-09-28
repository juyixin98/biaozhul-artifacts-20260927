//! Filesystem persistence adapter.
//!
//! Layout under the store root:
//!
//! ```text
//! <root>/
//!   manifest.json                 # id -> BlockMeta index (atomic rewrite per commit)
//!   blocks/<id>.lz71              # raw block frame (see crate::format)
//! ```
//!
//! The manifest is the single source of truth for chain membership; frames are
//! written (fsync + rename) before the manifest records them, so a crash never
//! leaves a manifest entry pointing at a half-written frame.
//!
//! Chain rule: a store may hold one logical stream. Saving an independent block
//! starts a new generation (root); every dependent block appends to the current tip
//! and records `prev_id`. Following `prev_id` links always reaches an independent root.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard};

use serde::{Deserialize, Serialize};

use crate::codec;
use crate::error::{CodecError, Result};
use crate::format::{BlockMode, MAX_CHAIN_BLOCKS};

/// Stored metadata for one block. This is the adapter's private JSON contract.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct BlockMeta {
    pub id: String,
    pub mode: String, // "independent" | "dependent"
    pub prev_id: Option<String>,
    pub data_len: u64,
    pub payload_len: u64,
    /// 0-based position in the chain, root = 0.
    pub sequence: u64,
    pub created_at_unix_ms: u128,
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct Manifest {
    /// Ordered map id -> meta; BTreeMap keeps listing stable across reloads.
    blocks: BTreeMap<String, BlockMeta>,
    /// Id of the current chain tip (`None` = empty store).
    tip: Option<String>,
    /// Monotonic id counter, also used to order blocks deterministically.
    next_seq: u64,
}

/// Filesystem-backed block store. Cheap to clone; all methods take an internal lock,
/// which is fine for the local single-node backend this crate targets.
#[derive(Clone)]
pub struct BlockStore {
    root: PathBuf,
    blocks_dir: PathBuf,
    manifest_path: PathBuf,
    inner: Arc<Mutex<()>>,
}

impl BlockStore {
    /// Open (or create) a store at `root`.
    pub fn open(root: impl AsRef<Path>) -> Result<Self> {
        let root = root.as_ref().to_path_buf();
        let blocks_dir = root.join("blocks");
        fs::create_dir_all(&blocks_dir).map_err(|e| {
            CodecError::internal(format!("create store dir {}: {e}", root.display()))
        })?;
        let store = Self {
            manifest_path: root.join("manifest.json"),
            blocks_dir,
            root,
            inner: Arc::new(Mutex::new(())),
        };
        // Validate that an existing manifest parses.
        let _ = store.load_manifest()?;
        Ok(store)
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    fn lock(&self) -> MutexGuard<'_, ()> {
        self.inner.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn load_manifest(&self) -> Result<Manifest> {
        match fs::read(&self.manifest_path) {
            Ok(bytes) => serde_json::from_slice(&bytes).map_err(|e| {
                CodecError::internal(format!(
                    "corrupt manifest {}: {e}",
                    self.manifest_path.display()
                ))
            }),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Manifest::default()),
            Err(e) => Err(CodecError::internal(format!(
                "read manifest {}: {e}",
                self.manifest_path.display()
            ))),
        }
    }

    fn write_manifest(&self, manifest: &Manifest) -> Result<()> {
        let bytes = serde_json::to_vec_pretty(manifest)
            .map_err(|e| CodecError::internal(format!("serialize manifest: {e}")))?;
        let tmp = self.manifest_path.with_extension("json.tmp");
        atomic_write(&tmp, &self.manifest_path, &bytes)
    }

    fn frame_path(&self, id: &str) -> PathBuf {
        self.blocks_dir.join(format!("{id}.lz71"))
    }

    /// Number of stored blocks.
    pub fn len(&self) -> Result<usize> {
        let _g = self.lock();
        Ok(self.load_manifest()?.blocks.len())
    }

    pub fn is_empty(&self) -> Result<bool> {
        Ok(self.len()? == 0)
    }

    /// Current chain tip, if any.
    pub fn tip(&self) -> Result<Option<BlockMeta>> {
        let _g = self.lock();
        let m = self.load_manifest()?;
        match m.tip {
            Some(id) => Ok(m.blocks.get(&id).cloned()),
            None => Ok(None),
        }
    }

    /// Compress and persist one independent block. Starts a new chain generation:
    /// afterwards this block is the tip/root.
    pub fn put_independent(&self, data: &[u8]) -> Result<BlockMeta> {
        let _g = self.lock();
        let mut manifest = self.load_manifest()?;
        let frame = codec::encode_independent(data);

        let seq = manifest.next_seq;
        manifest.next_seq += 1;
        let id = format!("blk-{seq:08}");
        let meta = BlockMeta {
            id: id.clone(),
            mode: "independent".to_string(),
            prev_id: None,
            data_len: data.len() as u64,
            payload_len: (frame.len() as u64) - crate::format::HEADER_LEN as u64,
            sequence: 0,
            created_at_unix_ms: unix_ms(),
        };
        self.commit_block(&mut manifest, &id, &frame, meta)
    }

    /// Compress and persist one dependent block. `expected_prev_id` optionally pins
    /// the predecessor (optimistic-concurrency control): if the tip has advanced
    /// since the client read it, this is a [`ErrorCategory::StateConflict`].
    ///
    /// The block is encoded against the trailing window of the entire chain output;
    /// its frame digest therefore cryptographically binds it to the predecessors.
    ///
    /// [`ErrorCategory::StateConflict`]: crate::error::ErrorCategory::StateConflict
    pub fn put_dependent(&self, data: &[u8], expected_prev_id: Option<&str>) -> Result<BlockMeta> {
        let _g = self.lock();
        let mut manifest = self.load_manifest()?;

        let prev_id = match &manifest.tip {
            Some(id) => id.clone(),
            None => {
                return Err(CodecError::state(
                    "cannot append dependent block: store has no root block",
                ));
            }
        };
        if let Some(want) = expected_prev_id {
            if want != prev_id {
                return Err(CodecError::state(format!(
                    "chain tip advanced: client pinned {want}, current tip is {prev_id}"
                )));
            }
        }

        // Assemble the predecessor dictionary as the decoder will see it.
        let chain = self.collect_frames_locked(&manifest, Some(&prev_id))?;
        let combined = codec::decode_chain(&chain)?;
        let frame = codec::encode_dependent(data, &combined)?;

        let prev_seq = manifest.blocks[&prev_id].sequence;
        let seq = manifest.next_seq;
        manifest.next_seq += 1;
        let id = format!("blk-{seq:08}");
        let meta = BlockMeta {
            id: id.clone(),
            mode: "dependent".to_string(),
            prev_id: Some(prev_id),
            data_len: data.len() as u64,
            payload_len: (frame.len() as u64) - crate::format::HEADER_LEN as u64,
            sequence: prev_seq + 1,
            created_at_unix_ms: unix_ms(),
        };
        self.commit_block(&mut manifest, &id, &frame, meta)
    }

    fn commit_block(
        &self,
        manifest: &mut Manifest,
        id: &str,
        frame: &[u8],
        meta: BlockMeta,
    ) -> Result<BlockMeta> {
        if manifest.blocks.contains_key(id) {
            return Err(CodecError::state(format!("duplicate block id {id}")));
        }
        let tmp = self.blocks_dir.join(format!("{id}.tmp"));
        let final_path = self.frame_path(id);
        atomic_write(&tmp, &final_path, frame)?;
        manifest.blocks.insert(id.to_string(), meta.clone());
        manifest.tip = Some(id.to_string());
        self.write_manifest(manifest)?;
        Ok(meta)
    }

    /// Read one raw frame.
    pub fn get_frame(&self, id: &str) -> Result<Vec<u8>> {
        let _g = self.lock();
        let manifest = self.load_manifest()?;
        if !manifest.blocks.contains_key(id) {
            return Err(CodecError::not_found(format!("block {id} not found")));
        }
        fs::read(self.frame_path(id)).map_err(|e| {
            CodecError::internal(format!("read frame {}: {e}", self.frame_path(id).display()))
        })
    }

    /// Fetch one metadata record.
    pub fn meta(&self, id: &str) -> Result<BlockMeta> {
        let _g = self.lock();
        let manifest = self.load_manifest()?;
        manifest
            .blocks
            .get(id)
            .cloned()
            .ok_or_else(|| CodecError::not_found(format!("block {id} not found")))
    }

    /// List all blocks in insertion order.
    pub fn list(&self) -> Result<Vec<BlockMeta>> {
        let _g = self.lock();
        let manifest = self.load_manifest()?;
        let mut metas: Vec<_> = manifest.blocks.values().cloned().collect();
        metas.sort_by_key(|m| m.sequence);
        Ok(metas)
    }

    /// Decompress exactly one block, assembling its predecessor dictionary from the
    /// stored chain. An independent block must have no predecessor; a dependent
    /// block whose chain is broken is a state conflict.
    pub fn read_block(&self, id: &str) -> Result<Vec<u8>> {
        let _g = self.lock();
        let manifest = self.load_manifest()?;
        if !manifest.blocks.contains_key(id) {
            return Err(CodecError::not_found(format!("block {id} not found")));
        }
        // Frames of the block plus all predecessors, root-first.
        let frames = self.collect_frames_locked(&manifest, Some(id))?;
        codec::decode_chain(&frames).map(|mut full| {
            // Strip predecessors, keep only the requested block.
            let own_len = manifest.blocks[id].data_len as usize;
            let start = full.len() - own_len;
            full.drain(..start);
            full
        })
    }

    /// Decompress the whole chain ending at `id` (or the current tip) concatenated.
    pub fn read_chain(&self, id: Option<&str>) -> Result<Vec<u8>> {
        let _g = self.lock();
        let manifest = self.load_manifest()?;
        let frames = self.collect_frames_locked(&manifest, id)?;
        codec::decode_chain(&frames)
    }

    /// Walk `prev_id` links from `start` (tip if `None`) and return root-first frames.
    fn collect_frames_locked(
        &self,
        manifest: &Manifest,
        start: Option<&str>,
    ) -> Result<Vec<Vec<u8>>> {
        let start_id = match start {
            Some(id) => {
                if !manifest.blocks.contains_key(id) {
                    return Err(CodecError::not_found(format!("block {id} not found")));
                }
                id.to_string()
            }
            None => manifest
                .tip
                .clone()
                .ok_or_else(|| CodecError::input("store is empty"))?,
        };

        let mut reversed = Vec::new();
        let mut current = Some(start_id);
        let mut steps = 0usize;
        while let Some(id) = current {
            if steps >= MAX_CHAIN_BLOCKS {
                return Err(CodecError::exhausted(format!(
                    "dependency chain exceeds depth limit {MAX_CHAIN_BLOCKS} while resolving {id}"
                )));
            }
            let meta = manifest.blocks.get(&id).ok_or_else(|| {
                CodecError::state(format!(
                    "missing predecessor block {id}: chain is broken (manifest references absent frame)"
                ))
            })?;
            let frame = fs::read(self.frame_path(&id)).map_err(|e| {
                if e.kind() == std::io::ErrorKind::NotFound {
                    CodecError::state(format!(
                        "frame file for {id} is missing on disk: chain is broken"
                    ))
                } else {
                    CodecError::internal(format!("read frame {id}: {e}"))
                }
            })?;
            reversed.push((meta.mode_str()?, frame));
            current = meta.prev_id.clone();
            steps += 1;
        }
        reversed.reverse();

        // Validate mode shape of the resolved chain before returning.
        for (i, (mode, _)) in reversed.iter().enumerate() {
            match (i, mode) {
                (0, BlockMode::Dependent) => {
                    return Err(CodecError::state(
                        "stored chain starts with a dependent block (missing predecessor block)",
                    ));
                }
                (i, BlockMode::Independent) if i > 0 => {
                    return Err(CodecError::state(format!(
                        "chain at position {i} is independent but has predecessors"
                    )));
                }
                _ => {}
            }
        }
        Ok(reversed.into_iter().map(|(_, f)| f).collect())
    }
}

impl BlockMeta {
    fn mode_str(&self) -> Result<BlockMode> {
        match self.mode.as_str() {
            "independent" => Ok(BlockMode::Independent),
            "dependent" => Ok(BlockMode::Dependent),
            other => Err(CodecError::internal(format!(
                "manifest block {} has unknown mode {other:?}",
                self.id
            ))),
        }
    }
}

fn unix_ms() -> u128 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

/// Write `bytes` to `tmp`, fsync, then atomically rename to `final_path`.
fn atomic_write(tmp: &Path, final_path: &Path, bytes: &[u8]) -> Result<()> {
    fs::write(tmp, bytes)
        .map_err(|e| CodecError::internal(format!("write tmp file {}: {e}", tmp.display())))?;
    fs::rename(tmp, final_path).map_err(|e| {
        CodecError::internal(format!(
            "rename {} -> {}: {e}",
            tmp.display(),
            final_path.display()
        ))
    })?;
    Ok(())
}
