//! Persistence tests: durable replay and explicit failure on corrupt state.
//!
//! Unknown/tampered state must surface as `persistence_error`, never be
//! silently repaired into a success.

mod common;

use common::caselog::CaseLog;
use prereg2d::errors::AppError;
use prereg2d::model::{PointUpdate, Rect};
use prereg2d::service::Registry;
use std::fs;
use std::path::Path;
use tempfile::TempDir;

fn pu(x: i64, y: i64, d: i64) -> PointUpdate {
    PointUpdate { x, y, delta: d }
}

fn whole() -> Rect {
    Rect {
        x_lo: i64::MIN as i128,
        x_hi: i64::MAX as i128,
        y_lo: i64::MIN as i128,
        y_hi: i64::MAX as i128,
    }
}

fn build_history(dir: &Path) {
    let reg = Registry::open(dir).unwrap();
    reg.register(vec![-3, 0, 3], vec![-3, 0, 3]).unwrap();
    reg.apply_batch(vec![pu(-3, -3, 10), pu(0, 0, -4), pu(3, 3, 2)])
        .unwrap();
    reg.apply_batch(vec![pu(-3, -3, -10), pu(3, 3, 1)]).unwrap();
    reg.rebuild(vec![0, 3, 9], vec![0, 3]).unwrap();
}

#[test]
fn replay_reconstructs_every_version() {
    let rid = common::caselog::run_id();
    let mut log = CaseLog::new(&rid, "replay-all-versions");
    let dir = TempDir::new().unwrap();
    build_history(dir.path());

    // Files must physically exist with checksummed lines.
    let log_path = dir.path().join("events.jsonl");
    let current_path = dir.path().join("CURRENT");
    let raw = fs::read_to_string(&log_path).unwrap();
    log.step(format!(
        "log bytes={} lines={}",
        raw.len(),
        raw.lines().count()
    ));
    for (i, line) in raw.lines().enumerate() {
        assert!(line.contains("\"hash\""), "line {} carries a hash", i + 1);
    }
    assert_eq!(fs::read_to_string(&current_path).unwrap().trim(), "4");

    // Reopen from disk: same head, same per-version answers, old versions too.
    let reopened = Registry::open(dir.path()).unwrap();
    let infos = reopened.list_versions();
    log.step(format!("replayed versions: {}", infos.len()));
    assert_eq!(infos.len(), 4);
    assert_eq!(reopened.head_version(), Some(4));

    // v2 total = 8, v3 total = -1 (-4+3), v4 carried -4+3 = -1.
    let expected = [(1, 0i128), (2, 8), (3, -1), (4, -1)];
    for (v, want) in expected {
        let got = reopened.query(Some(v), &whole()).unwrap().sum;
        log.step(format!("v{v} whole-plane: {got} (want {want})"));
        assert_eq!(got, want);
    }
    // v2 dropped-after-rebuild point present only in old version.
    assert_eq!(
        reopened
            .query(
                Some(2),
                &Rect {
                    x_lo: -3,
                    x_hi: -3,
                    y_lo: -3,
                    y_hi: -3
                }
            )
            .unwrap()
            .sum,
        10
    );
    assert_eq!(
        reopened
            .query(
                Some(4),
                &Rect {
                    x_lo: -3,
                    x_hi: -3,
                    y_lo: -3,
                    y_hi: -3
                }
            )
            .unwrap()
            .sum,
        0
    );
    log.assert_check(
        true,
        "replay matches all 4 versions incl. pre-rebuild history",
    );
}

#[test]
fn appended_after_reopen_continues_version_chain() {
    let rid = common::caselog::run_id();
    let mut log = CaseLog::new(&rid, "replay-then-append");
    let dir = TempDir::new().unwrap();
    build_history(dir.path());
    let reg = Registry::open(dir.path()).unwrap();
    // (0,0) = -4 and (3,3) = 3 carried into the rebuilt table; add +4 at 0,0.
    reg.apply_batch(vec![pu(0, 0, 4)]).unwrap();
    assert_eq!(reg.query(None, &whole()).unwrap().sum, 3);
    assert_eq!(reg.head_version(), Some(5));
    assert_eq!(
        fs::read_to_string(dir.path().join("CURRENT"))
            .unwrap()
            .trim(),
        "5"
    );

    let again = Registry::open(dir.path()).unwrap();
    let sum = again.query(Some(5), &whole()).unwrap().sum;
    log.step(format!("reopened v5 sum={sum}"));
    log.assert_check(sum == 3, "chain continues across restarts");
}

fn open_expect_persistence(dir: &Path, log: &mut CaseLog, name: &str) {
    match Registry::open(dir) {
        Err(AppError::Persistence(msg)) => {
            log.step(format!("{name}: correctly rejected: {msg}"));
            log.assert_check(true, format!("{name} -> persistence_error"));
        }
        other => panic!(
            "{name}: expected persistence error, got {:?}",
            other.map(|_| "Registry opened OK")
        ),
    }
}

fn copy_dir(src: &Path) -> TempDir {
    let dst = TempDir::new().unwrap();
    for entry in fs::read_dir(src).unwrap() {
        let e = entry.unwrap();
        fs::copy(e.path(), dst.path().join(e.file_name())).unwrap();
    }
    dst
}

#[test]
fn tampered_log_is_detected() {
    let rid = common::caselog::run_id();
    let mut log = CaseLog::new(&rid, "tamper-detection");
    let base = TempDir::new().unwrap();
    build_history(base.path());

    // 1. Flip one payload digit: checksum must mismatch.
    let t1 = copy_dir(base.path());
    let p = t1.path().join("events.jsonl");
    let s = fs::read_to_string(&p).unwrap();
    let tampered = s.replacen("\"delta\":10,", "\"delta\":11,", 1);
    assert_ne!(tampered, s, "fixture must actually change");
    fs::write(&p, tampered).unwrap();
    open_expect_persistence(t1.path(), &mut log, "checksum-flip");

    // 2. Truncate the log mid-line.
    let t2 = copy_dir(base.path());
    let p2 = t2.path().join("events.jsonl");
    let s2 = fs::read_to_string(&p2).unwrap();
    let cut = s2.len() - 3;
    fs::write(&p2, &s2[..cut]).unwrap();
    open_expect_persistence(t2.path(), &mut log, "truncated-tail");

    // 3. CURRENT points past the log tail.
    let t3 = copy_dir(base.path());
    fs::write(t3.path().join("CURRENT"), "400\n").unwrap();
    open_expect_persistence(t3.path(), &mut log, "current-ahead");

    // 4. CURRENT missing while the log has committed events.
    let t4 = copy_dir(base.path());
    fs::remove_file(t4.path().join("CURRENT")).unwrap();
    open_expect_persistence(t4.path(), &mut log, "current-missing");

    // 5. CURRENT is not a number.
    let t5 = copy_dir(base.path());
    fs::write(t5.path().join("CURRENT"), "not-a-version\n").unwrap();
    open_expect_persistence(t5.path(), &mut log, "current-garbage");

    // 6. Log exists but CURRENT says there is no history: covered by (4);
    //    an empty directory still opens cleanly (fresh start).
    let fresh = TempDir::new().unwrap();
    let reg = Registry::open(fresh.path()).unwrap();
    assert!(reg.head_version().is_none());
    log.step("empty directory opens as an uninitialized store");
    log.assert_check(true, "fresh dir accepted, every tamper case rejected");
}
