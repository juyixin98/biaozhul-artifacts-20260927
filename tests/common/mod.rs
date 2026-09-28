//! Shared integration-test harness:
//!
//! * [`oracle`] — drives the **independent Python reference** (`reference/ref_lz77.py`)
//!   so the Rust core is never used to verify itself;
//! * [`TestLog`] — per-run structured logs under `tests/test-logs/<run-id>/` recording
//!   the run number, inputs, key intermediate state and the judgement for each case.
//!
//! Logs are replay artefacts: every case entry contains the fixture path/parameters
//! and both sides' observations, so a failure can be reproduced without a debugger.

#![allow(dead_code)]

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

pub fn workspace_root() -> &'static Path {
    Path::new(env!("CARGO_MANIFEST_DIR"))
}

pub fn fixtures_dir() -> PathBuf {
    workspace_root().join("tests/fixtures")
}

pub fn fixture(rel: &str) -> Vec<u8> {
    fs::read(fixtures_dir().join(rel)).unwrap_or_else(|e| panic!("read fixture {rel}: {e}"))
}

pub fn source_bytes() -> Vec<u8> {
    fixture("source.bin")
}

pub fn fixtures_manifest() -> Value {
    let raw = fixture("fixtures_manifest.json");
    serde_json::from_slice(&raw).expect("fixtures manifest parses")
}

/// Fixture cases declared by the Python generator, keyed by `name`.
pub fn fixture_cases() -> std::collections::BTreeMap<String, Value> {
    fixtures_manifest()["cases"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| (c["name"].as_str().unwrap().to_string(), c.clone()))
        .collect()
}

// ------------------------------------------------------------------ Python oracle

#[derive(Debug, Clone)]
pub struct OracleReport {
    pub exit_code: i32,
    pub json: Value,
}

impl OracleReport {
    pub fn valid(&self) -> bool {
        self.json
            .get("valid")
            .and_then(Value::as_bool)
            .unwrap_or(false)
    }
    pub fn category(&self) -> Option<String> {
        self.json
            .get("error_category")
            .and_then(Value::as_str)
            .map(str::to_string)
    }
}

fn python_bin() -> Option<&'static str> {
    ["python3", "python"]
        .into_iter()
        .find(|&candidate| Command::new(candidate).arg("--version").output().is_ok())
        .map(|v| v as _)
}

static PYTHON: OnceLock<Option<&'static str>> = OnceLock::new();

/// Whether the independent Python reference is runnable in this environment.
pub fn oracle_available() -> bool {
    PYTHON.get_or_init(python_bin).is_some()
}

fn run_ref(args: &[&str]) -> OracleReport {
    let bin = PYTHON
        .get_or_init(python_bin)
        .expect("python3 is required to run the independent reference oracle; see tests/README.md");
    let script = workspace_root().join("reference/ref_lz77.py");
    let output = Command::new(bin)
        .arg(script)
        .args(args)
        .output()
        .expect("spawn reference interpreter");
    let json: Value = serde_json::from_slice(&output.stdout).unwrap_or_else(|e| {
        panic!(
            "reference emitted non-JSON stdout ({}): {e}\nstderr: {}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
    });
    OracleReport {
        exit_code: output.status.code().unwrap_or(-1),
        json,
    }
}

fn temp_path(tag: &str, ext: &str) -> PathBuf {
    let dir = workspace_root().join("target/test-tmp");
    fs::create_dir_all(&dir).unwrap();
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let pid = std::process::id();
    dir.join(format!("{tag}-{pid}-{nanos}{ext}"))
}

/// Decode a frame file through the Python reference. Output is always written to a
/// throwaway path under `target/test-tmp` (never next to a checked-in fixture).
/// Returns the JSON report and the decoded bytes (present iff validation succeeded).
pub fn oracle_decode_paths(frame_path: &Path, dictionary: &[u8]) -> (OracleReport, Option<Vec<u8>>) {
    let mut args: Vec<String> = vec![
        "decode".into(),
        "--in".into(),
        frame_path.display().to_string(),
    ];
    let dict_path;
    if !dictionary.is_empty() {
        dict_path = temp_path("oracle-dict", ".bin");
        fs::write(&dict_path, dictionary).unwrap();
        args.push("--dict".into());
        args.push(dict_path.display().to_string());
    }
    let out_path = temp_path("oracle-out", ".bin");
    args.push("--out".into());
    args.push(out_path.display().to_string());

    let bin = PYTHON.get_or_init(python_bin).expect("python3 required");
    let output = Command::new(bin)
        .arg(workspace_root().join("reference/ref_lz77.py"))
        .args(&args)
        .output()
        .expect("spawn reference interpreter");
    let json: Value = serde_json::from_slice(&output.stdout).unwrap_or_else(|e| {
        panic!(
            "reference non-JSON stdout: {e}\nstdout={}\nstderr={}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
    });
    let report = OracleReport {
        exit_code: output.status.code().unwrap_or(-1),
        json,
    };
    let out = report.valid().then(|| fs::read(&out_path).expect("oracle output file"));
    (report, out)
}

/// Round-trip helper: bytes -> temp frame file -> python decode. Returns
/// `(report, output_bytes_option)`.
pub fn oracle_roundtrip(frame: &[u8], dictionary: &[u8]) -> (OracleReport, Option<Vec<u8>>) {
    let p = temp_path("oracle-frame", ".frame");
    fs::write(&p, frame).unwrap();
    oracle_decode_paths(&p, dictionary)
}

/// Ask the independent reference to encode data (produces an independent frame).
pub fn oracle_encode(data: &[u8], mode: &str, dictionary: &[u8]) -> Vec<u8> {
    let inp = temp_path("oracle-in", ".bin");
    let out = temp_path("oracle-enc", ".frame");
    fs::write(&inp, data).unwrap();
    let mut cmd = Command::new(PYTHON.get_or_init(python_bin).expect("python3"));
    cmd.arg(workspace_root().join("reference/ref_lz77.py"))
        .args(["encode", "--in"])
        .arg(&inp)
        .args(["--out"])
        .arg(&out)
        .args(["--mode", mode]);
    if !dictionary.is_empty() {
        let dp = temp_path("oracle-edict", ".bin");
        fs::write(&dp, dictionary).unwrap();
        cmd.args(["--dict"]).arg(dp);
    }
    let status = cmd.status().expect("spawn reference encoder");
    assert!(status.success(), "reference encode failed");
    fs::read(out).unwrap()
}

// ---------------------------------------------------------------------- test logging

/// Structured case log. Every `#[test]` gets its own file
/// `<run-dir>/<suite>/<test-name>.json`, so tests running in parallel or living in
/// the same integration binary never overwrite one another; the aggregator merges
/// all files of a run.
pub struct TestLog {
    run_id: String,
    run_number: u64,
    log_dir: PathBuf,
    cases: Vec<Value>,
    binary: &'static str,
    /// Unique per #[test] within the binary (derived from the harness thread name).
    test_name: String,
}

fn current_test_name() -> String {
    // libtest names the worker thread after the test, e.g.
    // "service_api::failure_classes_map_...". Fall back to a timestamp.
    let raw = std::thread::current()
        .name()
        .unwrap_or("unknown_test")
        .to_string();
    raw.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '_' || c == '-' {
                c
            } else {
                '_'
            }
        })
        .collect()
}

impl TestLog {
    pub fn new(binary: &'static str) -> Self {
        let root = workspace_root().join("tests/test-logs");
        fs::create_dir_all(&root).unwrap();
        // Shared run id/number via env so all binaries launched by `run-tests.sh`
        // group together; standalone `cargo test` invents an ad-hoc id.
        let (run_id, run_number) = match std::env::var("LZ77_TEST_RUN_ID") {
            Ok(id) => {
                let n: u64 = std::env::var("LZ77_TEST_RUN_NUMBER")
                    .ok()
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(0);
                (id, n)
            }
            Err(_) => {
                let nanos = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_nanos();
                (format!("run-standalone-{nanos}"), 0)
            }
        };
        let log_dir = root.join(&run_id);
        fs::create_dir_all(&log_dir).unwrap();
        Self {
            run_id,
            run_number,
            log_dir,
            cases: Vec::new(),
            binary,
            test_name: current_test_name(),
        }
    }

    pub fn run_id(&self) -> &str {
        &self.run_id
    }

    /// Record one case. `judgement` is "PASS"/"FAIL"/"SKIP"; `state` holds the
    /// auditable intermediate observations (distances, digests, lengths, categories).
    pub fn record(&mut self, case: &str, judgement: &str, reason: &str, state: Value) {
        let entry = json!({
            "case": case,
            "judgement": judgement,
            "reason": reason,
            "binary": self.binary,
            "state": state,
            "wall_ns": SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos(),
        });
        self.cases.push(entry);
    }

    pub fn pass(&mut self, case: &str, reason: &str, state: Value) {
        self.record(case, "PASS", reason, state);
    }
    pub fn fail(&mut self, case: &str, reason: &str, state: Value) {
        self.record(case, "FAIL", reason, state);
    }
    pub fn skip(&mut self, case: &str, reason: &str, state: Value) {
        self.record(case, "SKIP", reason, state);
    }

    /// Record PASS when `ok`, otherwise FAIL; returns `ok` for chaining into assert!.
    pub fn pass_or_record(&mut self, ok: bool, case: &str, reason: &str, state: Value) -> bool {
        if ok {
            self.pass(case, reason, state);
        } else {
            self.fail(case, reason, state);
        }
        ok
    }

    pub fn finish(self) -> String {
        let summary = json!({
            "run_id": self.run_id,
            "run_number": self.run_number,
            "binary": self.binary,
            "test_name": self.test_name,
            "generated_at_unix_ms": SystemTime::now()
                .duration_since(UNIX_EPOCH).unwrap().as_millis(),
            "oracle": "reference/ref_lz77.py (independent brute-force + zlib + hashlib)",
            "counts": {
                "total": self.cases.len(),
                "pass": self.cases.iter().filter(|c| c["judgement"] == "PASS").count(),
                "fail": self.cases.iter().filter(|c| c["judgement"] == "FAIL").count(),
                "skip": self.cases.iter().filter(|c| c["judgement"] == "SKIP").count(),
            },
            "constants": {
                "window_bytes": lz77_blocks::format::WINDOW_BYTES,
                "min_match": lz77_blocks::format::MIN_MATCH,
                "max_match": lz77_blocks::format::MAX_MATCH,
                "max_output_bytes": lz77_blocks::format::MAX_OUTPUT_BYTES,
                "max_expansion_ratio": lz77_blocks::format::MAX_EXPANSION_RATIO,
            },
            "cases": self.cases,
        });
        let suite_dir = self.log_dir.join(self.binary);
        fs::create_dir_all(&suite_dir).unwrap();
        let path = suite_dir.join(format!("{}.json", self.test_name));
        fs::write(&path, serde_json::to_vec_pretty(&summary).unwrap()).unwrap();
        path.display().to_string()
    }
}

/// Hex helper for compact digest logging.
pub fn hex(bytes: &[u8]) -> String {
    lz77_blocks::codec::hex_encode(bytes)
}
