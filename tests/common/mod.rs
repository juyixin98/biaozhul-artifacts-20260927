//! 集成测试公共设施：
//! - [`TestLog`]：把“运行编号 + 输入夹具 + 关键中间状态 + 判断理由”实时追加到
//!   `test-logs/<binary>/<test>.log`，即使断言失败（panic）也已落盘，可按日志重放；
//! - [`harness`]：在临时目录上构造 IndexService + Axum app。
//!
//! 注意：朴素参照（ground truth）全部来自 `fm_index_service::naive`
//! 与手工硬编码夹具，**不由被测内核自己生成**。

#![allow(dead_code)]

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
};

use fm_index_service::config::IndexDefaults;
use fm_index_service::service::IndexService;

static SEQ: AtomicU64 = AtomicU64::new(0);

/// 单个测试用例的重放日志。
pub struct TestLog {
    /// 本次用例的运行编号（也会写进每条记录）。
    pub run_id: String,
    path: PathBuf,
    test_name: String,
}

impl TestLog {
    /// 在 `test-logs/<bin>/` 下打开（或新建）名为 `test_name` 的日志文件。
    pub fn new(test_name: &str) -> Self {
        let bin = std::env::args()
            .next()
            .and_then(|p| {
                std::path::Path::new(&p)
                    .file_stem()
                    .map(|s| s.to_string_lossy().into_owned())
            })
            .unwrap_or_else(|| "unknown".to_string());
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("test-logs")
            .join(&bin);
        fs::create_dir_all(&dir).expect("创建 test-logs 目录");
        let path = dir.join(format!("{test_name}.log"));
        let run_id = format!(
            "it-{}-{}-{:04}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
            SEQ.fetch_add(1, Ordering::Relaxed)
        );
        let mut f = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .expect("打开测试日志");
        writeln!(f, "\n===== RUN {run_id} test={test_name} =====").unwrap();
        TestLog {
            run_id,
            path,
            test_name: test_name.to_string(),
        }
    }

    /// 记录一行结构化信息（key=value 风格，值用 debug 输出）。
    pub fn record(&mut self, key: impl AsRef<str>, value: impl std::fmt::Debug) {
        let line = format!("[{}] {} = {value:?}\n", self.run_id, key.as_ref());
        let mut f = OpenOptions::new()
            .append(true)
            .open(&self.path)
            .expect("打开测试日志");
        f.write_all(line.as_bytes()).expect("写测试日志");
        // 立即落盘：panic/abort 后仍可重放。
        let _ = f.sync_all();
        print!("{line}");
    }

    /// 记录自由文本判断理由。
    pub fn note(&mut self, msg: impl AsRef<str>) {
        let line = format!("[{}] note: {}\n", self.run_id, msg.as_ref());
        let mut f = OpenOptions::new()
            .append(true)
            .open(&self.path)
            .expect("打开测试日志");
        f.write_all(line.as_bytes()).expect("写测试日志");
        let _ = f.sync_all();
        print!("{line}");
    }

    /// 断言并把理由写入日志（成功也记录，形成完整判断链）。
    pub fn assert_eq_log<T: PartialEq + std::fmt::Debug>(
        &mut self,
        actual: &T,
        expected: &T,
        what: impl AsRef<str>,
    ) {
        let what = what.as_ref();
        let ok = actual == expected;
        self.record(
            if ok {
                format!("ASSERT_OK {what}")
            } else {
                format!("ASSERT_FAIL {what}")
            },
            (&actual, &expected),
        );
        assert!(
            ok,
            "断言失败（{what}）actual={actual:?} expected={expected:?}；日志见 {}",
            self.path.display()
        );
    }
}

/// 测试用服务 + 临时目录句柄（删除即清理）。
pub struct Harness {
    pub service: Arc<IndexService>,
    pub _data: tempfile::TempDir,
    pub import: tempfile::TempDir,
}

impl Harness {
    pub fn new() -> Self {
        let data = tempfile::tempdir().expect("临时数据目录");
        let import = tempfile::tempdir().expect("临时导入目录");
        let svc = Arc::new(IndexService::new(
            data.path().to_path_buf(),
            vec![import.path().to_path_buf()],
            IndexDefaults {
                max_text_bytes: 4 * 1024 * 1024,
                rank_block: 256,
                sample_step: 16,
            },
            100_000,
        ));
        Harness {
            service: svc,
            _data: data,
            import,
        }
    }

    /// 直接在内核层建一个已持久化+已加载索引。
    pub fn build_index(&self, name: &str, text: &[u8], rank_block: u32, sample_step: u32) {
        self.service
            .create_from_text(name, text.to_vec(), Some(rank_block), Some(sample_step))
            .expect("建索引成功");
    }

    /// 构造 Axum app（one-shot 测试用）。
    pub fn app(&self) -> axum::Router {
        fm_index_service::api::app(self.service.clone(), 8 * 1024 * 1024)
    }
}

/// 把字节渲染成可读形式（ASCII 直接显示，其它走 hex），供日志阅读。
pub fn preview(bytes: &[u8], limit: usize) -> String {
    if bytes.len() <= limit && bytes.iter().all(|b| (32..=126).contains(b)) {
        format!("{:?}", std::str::from_utf8(bytes).unwrap())
    } else {
        format!(
            "<{} bytes, hex-head={}>",
            bytes.len(),
            hex_head(bytes, limit)
        )
    }
}

fn hex_head(bytes: &[u8], n: usize) -> String {
    bytes
        .iter()
        .take(n)
        .map(|b| format!("{b:02x}"))
        .collect::<Vec<_>>()
        .join(" ")
}

/// 确定性 LCG 伪随机字节（与内核测试同序列风格，参数独立）。
pub fn pseudo_bytes(seed: u32, len: usize, alpha: u8) -> Vec<u8> {
    let mut s = seed ^ 0x9e37_79b9;
    let mut out = Vec::with_capacity(len);
    for _ in 0..len {
        s ^= s << 13;
        s ^= s >> 17;
        s ^= s << 5;
        out.push((s >> 8) as u8 % alpha);
    }
    out
}
