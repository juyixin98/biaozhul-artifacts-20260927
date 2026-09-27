//! Atomic-publication tests.
//!
//! While batches are being applied concurrently, readers must only ever
//! observe fully published versions whose rectangle totals are internally
//! consistent (they must equal one of the oracle's known per-version full
//! scans — never a half-applied intermediate).

mod common;

use common::oracle::{FullScanOracle, ORect, OUpdate};
use prereg2d::model::{PointUpdate, Rect};
use prereg2d::service::Registry;
use std::collections::HashSet;
use std::sync::Arc;
use std::thread;
use tempfile::TempDir;

fn pu(x: i64, y: i64, d: i64) -> PointUpdate {
    PointUpdate { x, y, delta: d }
}

#[test]
fn concurrent_readers_never_see_half_batch() {
    let rid = common::caselog::run_id();
    let mut log = common::caselog::CaseLog::new(&rid, "atomic-publication");

    let dir = TempDir::new().unwrap();
    let reg = Arc::new(Registry::open(dir.path()).unwrap());

    // 8 registered points in a row along the diagonal band.
    let coords: Vec<(i64, i64)> = (0..8).map(|i| (i as i64, i as i64)).collect();
    reg.register(
        coords.iter().map(|c| c.0).collect(),
        coords.iter().map(|c| c.1).collect(),
    )
    .unwrap();

    // Single-writer deterministic batch stream: each batch adds +1 to every
    // registered point, so at version v every point equals (v-1) and the
    // whole-plane total is exactly 8*(v-1). A torn/half publication would show
    // a total that is not a multiple of 8 in [0, 8*BATCHES].
    const BATCHES: u64 = 300;
    let writer = {
        let reg = reg.clone();
        thread::spawn(move || {
            for b in 1..=BATCHES {
                let ups: Vec<PointUpdate> = coords.iter().map(|&(x, y)| pu(x, y, 1)).collect();
                reg.apply_batch(ups).unwrap();
                if b % 50 == 0 {
                    log_safe(&format!("writer published v{}", b + 1));
                }
            }
        })
    };

    // Readers: hammer head queries and validate the invariant against the set
    // of legal published totals {0,8,...,8*BATCHES}.
    let mut readers = Vec::new();
    for reader_id in 0..6 {
        let reg = reg.clone();
        let rid2 = rid.clone();
        readers.push(thread::spawn(move || {
            let whole = Rect {
                x_lo: i64::MIN as i128,
                x_hi: i64::MAX as i128,
                y_lo: i64::MIN as i128,
                y_hi: i64::MAX as i128,
            };
            let mut checks = 0u64;
            loop {
                let head = match reg.head_version() {
                    Some(h) => h,
                    None => continue,
                };
                if head > BATCHES + 1 {
                    break;
                }
                let out = reg.query(None, &whole).unwrap();
                let sum = out.sum;
                let legal = (0..=BATCHES).map(|k| 8 * k as i128).collect::<HashSet<_>>();
                if !legal.contains(&sum) {
                    panic!(
                        "[{rid2}] reader {reader_id}: TORN READ head={} sum={sum} (not a multiple of 8)",
                        out.version
                    );
                }
                // The reported version must itself agree with its sum:
                // version v => sum 8*(v-1).
                if sum != 8 * (out.version as i128 - 1) {
                    panic!(
                        "[{rid2}] reader {reader_id}: inconsistent snapshot version={} sum={sum}",
                        out.version
                    );
                }
                checks += 1;
                if head == BATCHES + 1 {
                    break;
                }
            }
            checks
        }));
    }

    writer.join().unwrap();
    let total_checks: u64 = readers.into_iter().map(|j| j.join().unwrap()).sum();
    log.step(format!(
        "{total_checks} concurrent head queries, all matched a fully published version"
    ));
    let final_sum = reg
        .query(
            None,
            &Rect {
                x_lo: i64::MIN as i128,
                x_hi: i64::MAX as i128,
                y_lo: i64::MIN as i128,
                y_hi: i64::MAX as i128,
            },
        )
        .unwrap()
        .sum;
    log.assert_check(
        final_sum == 8 * BATCHES as i128,
        format!("final total {final_sum}"),
    );
}

/// A rejected batch must leave both the head version and totals unchanged —
/// the service-level atomicity guarantee for validation failures.
#[test]
fn rejected_batch_publishes_nothing() {
    let rid = common::caselog::run_id();
    let mut log = common::caselog::CaseLog::new(&rid, "rejected-batch-atomic");
    let dir = TempDir::new().unwrap();
    let reg = Registry::open(dir.path()).unwrap();
    let mut orc = FullScanOracle::new();
    reg.register(vec![1, 2], vec![1, 2]).unwrap();
    orc.register(&[1, 2], &[1, 2]).unwrap();
    reg.apply_batch(vec![pu(1, 1, 10)]).unwrap();
    orc.batch(&[OUpdate {
        x: 1,
        y: 1,
        delta: 10,
    }])
    .unwrap();

    let bad: Vec<PointUpdate> = vec![pu(1, 1, 1), pu(99, 99, 1), pu(2, 2, 1)];
    let err = reg.apply_batch(bad).unwrap_err();
    assert_eq!(err.code(), "unregistered_coordinate");
    assert_eq!(reg.head_version().unwrap(), 2, "no version published");

    // Even the registered entries of the rejected batch must not have landed.
    let whole = Rect {
        x_lo: i64::MIN as i128,
        x_hi: i64::MAX as i128,
        y_lo: i64::MIN as i128,
        y_hi: i64::MAX as i128,
    };
    let sum = reg.query(Some(2), &whole).unwrap().sum;
    let (want, _, _) = orc.query(
        2,
        ORect {
            x_lo: i64::MIN as i128,
            x_hi: i64::MAX as i128,
            y_lo: i64::MIN as i128,
            y_hi: i64::MAX as i128,
        },
    );
    log.step(format!("post-reject total impl={sum} oracle={want}"));
    log.assert_check(sum == want && sum == 10, "partial batch effects are absent");
}

fn log_safe(msg: &str) {
    eprintln!("    writer: {msg}");
}
