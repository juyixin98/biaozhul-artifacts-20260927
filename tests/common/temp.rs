//! 测试临时目录（不依赖 tempfile）。默认测试结束清理；
//! `PR2D_KEEP_TMP=1` 保留（如 `PR2D_KEEP_TMP=1 cargo test`）。

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

static COUNTER: AtomicU64 = AtomicU64::new(0);

pub struct TempDir {
    path: PathBuf,
    keep: bool,
}

impl TempDir {
    pub fn new(prefix: &str) -> TempDir {
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "pr2dtest-{prefix}-pid{}-{nanos}-{n}",
            std::process::id()
        ));
        std::fs::create_dir_all(&path).expect("create temp dir");
        let keep = std::env::var("PR2D_KEEP_TMP").is_ok();
        TempDir { path, keep }
    }

    pub fn path(&self) -> &std::path::Path {
        &self.path
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        if !self.keep {
            let _ = std::fs::remove_dir_all(&self.path);
        } else {
            eprintln!("PR2D_KEEP_TMP set; leaving {}", self.path.display());
        }
    }
}
