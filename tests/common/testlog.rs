//! 失败复现日志：记录运行身份、测试用例、输入、版本、计算步骤与判定依据。
//!
//! 写入位置：`target/pr2d-test-logs/<run_id>/<case>.jsonl`
//! （cargo 测试的 CWD 是 crate 根）。`PR2D_KEEP_TMP=1` 时路径也在失败输出中打印。
//!
//! 每条日志一行 JSON：
//! ```json
//! {"at":"assert","case":"...","request_id":"...","step":"...","input":{...},
//!  "expected":{...},"actual":{...},"verdict":"pass|fail"}
//! ```

use std::fs::{create_dir_all, File, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

use serde_json::Value;

static RUN_ID: std::sync::OnceLock<String> = std::sync::OnceLock::new();
static SEQ: AtomicU64 = AtomicU64::new(0);

pub fn run_id() -> String {
    RUN_ID
        .get_or_init(|| {
            let nanos = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            format!(
                "testrun-{:016x}-pid{}",
                nanos as u64 ^ ((std::process::id() as u64) << 17),
                std::process::id()
            )
        })
        .clone()
}

pub struct TestLog {
    case: String,
    file: Option<File>,
    dir: PathBuf,
}

impl TestLog {
    pub fn new(case: &str) -> TestLog {
        let dir = PathBuf::from("target/pr2d-test-logs").join(run_id());
        let file = if create_dir_all(&dir).is_ok() {
            let p = dir.join(format!("{}.jsonl", safe(case)));
            OpenOptions::new().create(true).append(true).open(p).ok()
        } else {
            None
        };
        let mut l = TestLog {
            case: case.to_string(),
            file,
            dir,
        };
        l.write(
            "case_start",
            serde_json::json!({
                "crate": env!("CARGO_PKG_NAME"),
                "version": env!("CARGO_PKG_VERSION"),
                "thread": format!("{:?}", std::thread::current().id()),
            }),
            Value::Null,
            Value::Null,
            "start",
        );
        l
    }

    pub fn dir(&self) -> PathBuf {
        self.dir.clone()
    }

    /// 记录一个判定。`verdict=false` 会在消息中给出输入/期望/实际与 request_id。
    pub fn record(
        &mut self,
        step: &str,
        input: Value,
        expected: Value,
        actual: Value,
        verdict: bool,
    ) -> &mut Self {
        self.write(
            step,
            input,
            expected,
            actual,
            if verdict { "pass" } else { "FAIL" },
        );
        self
    }

    /// 断言并记录；失败信息携带 request_id、步骤、输入、期望、实际。
    pub fn assert_eq_json(&mut self, step: &str, input: Value, expected: Value, actual: Value) {
        let ok = expected == actual;
        self.record(step, input.clone(), expected.clone(), actual.clone(), ok);
        assert!(
            ok,
            "[{}] step={step}\n  run_id={}\n  request_id={}\n  input={}\n  expected={}\n  actual={}\n  log_dir={}",
            self.case,
            run_id(),
            self.request_id(step),
            serde_json::to_string(&input).unwrap_or_default(),
            serde_json::to_string_pretty(&expected).unwrap_or_default(),
            serde_json::to_string_pretty(&actual).unwrap_or_default(),
            self.dir.display(),
        );
    }

    /// 子集断言：`expected` 中的每个键值（含嵌套对象逐层）都必须在 `actual` 中相等；
    /// `actual` 允许携带额外字段（如服务端生成的时间戳）。数组按 JSON 相等比较。
    pub fn assert_subset_json(&mut self, step: &str, input: Value, expected: Value, actual: Value) {
        let missing = subset_diff(&expected, &actual);
        let ok = missing.is_empty();
        self.record(step, input.clone(), expected.clone(), actual.clone(), ok);
        assert!(
            ok,
            "[{}] step={step}\n  run_id={}\n  request_id={}\n  input={}\n  expected_subset={}\n  actual={}\n  mismatches={:?}\n  log_dir={}",
            self.case,
            run_id(),
            self.request_id(step),
            serde_json::to_string(&input).unwrap_or_default(),
            serde_json::to_string_pretty(&expected).unwrap_or_default(),
            serde_json::to_string_pretty(&actual).unwrap_or_default(),
            missing,
            self.dir.display(),
        );
    }

    /// 与一次请求关联的稳定 request_id（用例名 + 序号），服务端日志可据此交叉检索。
    pub fn request_id(&self, step: &str) -> String {
        let n = SEQ.fetch_add(1, Ordering::Relaxed);
        format!("{}-{:04}-{}", safe(&self.case), n, safe(step))
    }

    #[allow(clippy::too_many_arguments)]
    fn write(&mut self, step: &str, input: Value, expected: Value, actual: Value, verdict: &str) {
        let n = SEQ.fetch_add(1, Ordering::Relaxed);
        let line = serde_json::json!({
            "at": "assert",
            "case": self.case,
            "run_id": run_id(),
            "request_id": format!("{}-{:04}-{}", safe(&self.case), n, safe(step)),
            "step": step,
            "input": input,
            "expected": expected,
            "actual": actual,
            "verdict": verdict,
        });
        if let Some(f) = self.file.as_mut() {
            let _ = writeln!(f, "{}", serde_json::to_string(&line).unwrap_or_default());
            let _ = f.flush();
        }
    }
}

fn safe(s: &str) -> String {
    s.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '-'
            }
        })
        .collect()
}

/// 递归子集差异：返回不满足 `expected ⊆ actual` 的 JSON 指针与说明。
fn subset_diff(expected: &Value, actual: &Value) -> Vec<String> {
    let mut diffs = Vec::new();
    diff_walk(expected, actual, String::new(), &mut diffs);
    diffs
}

fn diff_walk(expected: &Value, actual: &Value, path: String, out: &mut Vec<String>) {
    match (expected, actual) {
        (Value::Object(want), Value::Object(have)) => {
            for (k, wv) in want {
                let p = if path.is_empty() {
                    format!("/{k}")
                } else {
                    format!("{path}/{k}")
                };
                match have.get(k) {
                    Some(av) => diff_walk(wv, av, p, out),
                    None => out.push(format!("{p}: missing in actual")),
                }
            }
        }
        _ => {
            if expected != actual {
                out.push(format!("{path}: want {expected}, got {actual}"));
            }
        }
    }
}
