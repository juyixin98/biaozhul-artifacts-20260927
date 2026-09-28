//! `hbs-store`: filesystem persistence adapter.
//!
//! Each named set is one `<data_dir>/<name>.hbs` file encoded with
//! `hbs-format`. Writes are atomic: bytes go to a temporary file in the same
//! directory, are fsynced, then renamed over the target and (optionally) the
//! directory itself is fsynced, so a crash never leaves a truncated live
//! file. Reads decode through the validating format layer, which means a
//! corrupt file surfaces as a specific [`hbs_format::FormatError`].
//!
//! Set names are validated identifiers; path traversal is impossible
//! because a name can never contain a path separator or escape to an
//! absolute path.

use std::fs;
use std::io::BufWriter;
use std::path::{Path, PathBuf};

use hbs_core::HierBitmap;
use hbs_format::{FormatError, decode_from, encode_to};

/// File extension used for every set file.
pub const EXTENSION: &str = "hbs";

/// Filesystem-backed set store.
#[derive(Debug, Clone)]
pub struct FileStore {
    root: PathBuf,
    sync_writes: bool,
}

/// Summary of one stored set.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SetInfo {
    /// Set name (without extension).
    pub name: String,
    /// File size in bytes.
    pub file_size: u64,
    /// Last modification time as Unix seconds.
    pub modified_unix: i64,
}

/// Store-level failures.
#[derive(Debug)]
pub enum StoreError {
    /// Name is not a legal identifier.
    BadName(String),
    /// Set already exists (on strict create).
    AlreadyExists(String),
    /// Set does not exist.
    NotFound(String),
    /// Wrapped I/O failure.
    Io(std::io::Error),
    /// File on disk is malformed or corrupted.
    Corrupt(FormatError),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::BadName(n) => write!(f, "illegal set name: {n:?}"),
            StoreError::AlreadyExists(n) => write!(f, "set already exists: {n}"),
            StoreError::NotFound(n) => write!(f, "set not found: {n}"),
            StoreError::Io(e) => write!(f, "filesystem error: {e}"),
            StoreError::Corrupt(e) => write!(f, "corrupt set file: {e}"),
        }
    }
}

impl std::error::Error for StoreError {}

impl From<std::io::Error> for StoreError {
    fn from(e: std::io::Error) -> Self {
        StoreError::Io(e)
    }
}

impl From<FormatError> for StoreError {
    fn from(e: FormatError) -> Self {
        StoreError::Corrupt(e)
    }
}

impl FileStore {
    /// Open (and, if needed, create) a store rooted at `root`.
    pub fn open(root: impl AsRef<Path>, sync_writes: bool) -> Result<Self, StoreError> {
        let root = root.as_ref().to_path_buf();
        fs::create_dir_all(&root)?;
        Ok(Self { root, sync_writes })
    }

    /// Root directory.
    pub fn root(&self) -> &Path {
        &self.root
    }

    fn path_for(&self, name: &str) -> Result<PathBuf, StoreError> {
        validate_name(name)?;
        Ok(self.root.join(format!("{name}.{EXTENSION}")))
    }

    /// List stored sets, sorted by name. Each file is stat'd but not read.
    pub fn list(&self) -> Result<Vec<SetInfo>, StoreError> {
        let mut out = Vec::new();
        for entry in fs::read_dir(&self.root)? {
            let entry = entry?;
            let path = entry.path();
            if path.extension().and_then(|e| e.to_str()) != Some(EXTENSION) {
                continue;
            }
            let name = path
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or_default()
                .to_string();
            if validate_name(&name).is_err() {
                // Skip files we would never have written (e.g. the temp
                // files of a crashed process are cleaned separately).
                continue;
            }
            let meta = entry.metadata()?;
            let modified = meta
                .modified()?
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs() as i64)
                .unwrap_or(0);
            out.push(SetInfo {
                name,
                file_size: meta.len(),
                modified_unix: modified,
            });
        }
        out.sort_by(|a, b| a.name.cmp(&b.name));
        Ok(out)
    }

    /// Load and fully validate a set.
    pub fn load(&self, name: &str) -> Result<HierBitmap, StoreError> {
        let path = self.path_for(name)?;
        if !path.exists() {
            return Err(StoreError::NotFound(name.to_string()));
        }
        let file = fs::File::open(&path)?;
        let decoded = decode_from(std::io::BufReader::new(file))?;
        Ok(decoded.set)
    }

    /// Validate without returning the set: cheap health check used by the
    /// verification endpoint and the corruption test suite.
    pub fn verify(&self, name: &str) -> Result<hbs_format::DecodedFile, StoreError> {
        let path = self.path_for(name)?;
        if !path.exists() {
            return Err(StoreError::NotFound(name.to_string()));
        }
        let file = fs::File::open(&path)?;
        Ok(decode_from(std::io::BufReader::new(file))?)
    }

    /// Atomically persist a set, replacing any existing file.
    pub fn save(&self, name: &str, set: &HierBitmap) -> Result<(), StoreError> {
        let target = self.path_for(name)?;
        let tmp = self.root.join(format!(".{name}.{EXTENSION}.tmp"));
        {
            let file = fs::File::create(&tmp)?;
            let mut writer = BufWriter::new(file);
            encode_to(set, &mut writer)?;
            let file = writer
                .into_inner()
                .map_err(|e| StoreError::Io(e.into_error()))?;
            file.sync_all()?;
        }
        fs::rename(&tmp, &target)?;
        if self.sync_writes {
            // fsync the directory so the rename itself is durable.
            if let Ok(dir) = fs::File::open(&self.root) {
                let _ = dir.sync_all();
            }
        }
        Ok(())
    }

    /// Create only if absent.
    pub fn create(&self, name: &str, set: &HierBitmap) -> Result<(), StoreError> {
        let path = self.path_for(name)?;
        if path.exists() {
            return Err(StoreError::AlreadyExists(name.to_string()));
        }
        self.save(name, set)
    }

    /// Delete a set.
    pub fn delete(&self, name: &str) -> Result<(), StoreError> {
        let path = self.path_for(name)?;
        match fs::remove_file(&path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                Err(StoreError::NotFound(name.to_string()))
            }
            Err(e) => Err(StoreError::Io(e)),
        }
    }
}

/// Names are `[A-Za-z0-9_-]` starting with a letter or digit, 1..=64 chars.
/// This forbids `/`, `\`, `.`, `..`, absolute paths and NUL bytes.
pub fn validate_name(name: &str) -> Result<(), StoreError> {
    let ok = !name.is_empty()
        && name.len() <= 64
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
        && name
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_alphanumeric());
    if ok {
        Ok(())
    } else {
        Err(StoreError::BadName(name.to_string()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn name_validation_blocks_traversal() {
        for bad in [
            "",
            "a/b",
            "..",
            "../x",
            "a\\b",
            "a.b",
            "/abs",
            "a\0b",
            &"x".repeat(65),
        ] {
            assert!(
                matches!(validate_name(bad), Err(StoreError::BadName(_))),
                "{bad:?}"
            );
        }
        for good in ["a", "A1", "set-2_x", &"n".repeat(64)] {
            assert!(validate_name(good).is_ok(), "{good:?}");
        }
    }

    #[test]
    fn save_load_roundtrip_and_replace() {
        let dir = std::env::temp_dir().join(format!("hbs-store-rt-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let store = FileStore::open(&dir, false).unwrap();

        let mut s = HierBitmap::new();
        for v in [0u32, 1, 65536, u32::MAX] {
            s.insert(v);
        }
        store.save("alpha", &s).unwrap();
        assert_eq!(store.load("alpha").unwrap(), s);

        // replace
        let mut s2 = HierBitmap::new();
        s2.insert(7);
        store.save("alpha", &s2).unwrap();
        assert_eq!(store.load("alpha").unwrap(), s2);

        // create must refuse to overwrite
        assert!(matches!(
            store.create("alpha", &s),
            Err(StoreError::AlreadyExists(_))
        ));

        let names: Vec<String> = store.list().unwrap().into_iter().map(|i| i.name).collect();
        assert_eq!(names, vec!["alpha".to_string()]);

        store.delete("alpha").unwrap();
        assert!(matches!(store.load("alpha"), Err(StoreError::NotFound(_))));
        let _ = fs::remove_dir_all(&dir);
    }
}
