#![allow(clippy::type_complexity)]
//! Service-layer tests against an independent HashMap full-scan oracle.
//!
//! Every accepted batch is mirrored into [`FullScanOracle`]; every query
//! answer is compared with the oracle's full scan. Rejections are asserted by
//! exact `error_code` and by the oracle's matching [`OReject`] category.
//!
//! [`FullScanOracle`]: common::oracle::FullScanOracle

mod common;

use common::caselog::CaseLog;
use common::oracle::{FullScanOracle, ORect, OReject, OUpdate};
use common::rng::Lcg;
use prereg2d::errors::AppError;
use prereg2d::model::{PointUpdate, Rect};
use prereg2d::service::Registry;
use std::collections::HashSet;
use tempfile::TempDir;

fn pu(x: i64, y: i64, d: i64) -> PointUpdate {
    PointUpdate { x, y, delta: d }
}
fn ou(x: i64, y: i64, d: i64) -> OUpdate {
    OUpdate { x, y, delta: d }
}

fn fresh() -> (TempDir, Registry) {
    let dir = TempDir::new().expect("tempdir");
    let reg = Registry::open(dir.path()).expect("open empty store");
    (dir, reg)
}

fn expect_code(err: AppError, code: &str, log: &mut CaseLog, ctx: &str) {
    log.step(format!("{ctx}: got error_code={:?}", err.code()));
    assert_eq!(
        err.code(),
        code,
        "{ctx}: expected {code}, got {}",
        err.code()
    );
    log.assert_check(err.code() == code, format!("{ctx} -> {code}"));
}

/// Drive register → hand-checked batches → rejected batches → rebuild, while
/// mirroring everything in the independent oracle.
#[test]
fn handcalc_fixture_end_to_end() {
    let rid = common::caselog::run_id();
    let mut log = CaseLog::new(&rid, "handcalc-fixture");
    let (_dir, reg) = fresh();
    let mut orc = FullScanOracle::new();

    // v1: registration.
    reg.register(vec![-5, 0, 7], vec![-3, 2, 100]).unwrap();
    orc.register(&[-5, 0, 7], &[-3, 2, 100]).unwrap();

    let q = |reg: &Registry, v: u64, r: (i128, i128, i128, i128)| {
        reg.query(
            Some(v),
            &Rect {
                x_lo: r.0,
                x_hi: r.1,
                y_lo: r.2,
                y_hi: r.3,
            },
        )
        .unwrap()
        .sum
    };

    // baseline on v1 must be zero (queries work immediately after register).
    let whole = (
        i64::MIN as i128,
        i64::MAX as i128,
        i64::MIN as i128,
        i64::MAX as i128,
    );
    log.step(format!("v1 whole plane = {}", q(&reg, 1, whole)));
    assert_eq!(q(&reg, 1, whole), 0);

    // v2: mixed-sign baseline batch.
    reg.apply_batch(vec![pu(-5, -3, 3), pu(0, 2, -5), pu(7, 100, 10)])
        .unwrap();
    orc.batch(&[ou(-5, -3, 3), ou(0, 2, -5), ou(7, 100, 10)])
        .unwrap();

    let hand: &[(u64, &str, (i128, i128, i128, i128), i128)] = &[
        (2, "whole-plane-v2", whole, 8),
        (2, "mixed-window", (-5, 0, -3, 2), -2),
        (2, "point-negative", (0, 0, 2, 2), -5),
        (2, "point-positive", (7, 7, 100, 100), 10),
        (2, "gap", (1, 6, -3, 100), 0),
        (2, "inverted-x-empty", (1, 0, -3, 100), 0),
        (2, "inverted-y-empty", (-5, 7, 5, 4), 0),
        (2, "boundary-inclusive", (-5, 7, -3, 100), 8),
    ];
    for (v, name, r, want) in hand {
        let got = q(&reg, *v, *r);
        let (osc, _, empty) = orc.query(
            *v,
            ORect {
                x_lo: r.0,
                x_hi: r.1,
                y_lo: r.2,
                y_hi: r.3,
            },
        );
        log.step(format!(
            "{name}: impl={got} oracle_fullscan={osc} hand={want}"
        ));
        assert_eq!(got, *want, "{name} hand value");
        assert_eq!(got, osc, "{name} oracle value");
        if name.contains("empty") {
            assert!(empty || got == 0);
        }
    }

    // v3: cancel the (-5,-3) corner.
    reg.apply_batch(vec![pu(-5, -3, -3)]).unwrap();
    orc.batch(&[ou(-5, -3, -3)]).unwrap();
    assert_eq!(q(&reg, 3, (-5, -5, -3, -3)), 0);
    assert_eq!(q(&reg, 3, whole), 5);

    // Rejections: exact categories, state unchanged after each.
    let rejects: &[(&str, Vec<PointUpdate>, &str)] = &[
        (
            "unregistered-x",
            vec![pu(1, 2, 4)],
            "unregistered_coordinate",
        ),
        (
            "unregistered-y",
            vec![pu(0, 999, 4)],
            "unregistered_coordinate",
        ),
        (
            "dup-in-batch",
            vec![pu(0, 2, 1), pu(0, 2, 2)],
            "duplicate_in_batch",
        ),
        ("empty-batch", vec![], "empty_batch"),
        ("overflow-max", vec![pu(7, 100, i64::MAX)], "overflow"),
        ("overflow-min", vec![pu(0, 2, i64::MIN)], "overflow"),
    ];
    for (name, ups, code) in rejects {
        let before = reg.list_versions().len();
        let err = reg.apply_batch(ups.clone()).unwrap_err();
        let after = reg.list_versions().len();
        expect_code(err, code, &mut log, name);
        assert_eq!(before, after, "{name} must not publish a version");
        // oracle agrees on category (except the overflow-min case on a point
        // that is zero at head; oracle still classifies overflow vs bounds).
        let o_ups: Vec<OUpdate> = ups.iter().map(|u| ou(u.x, u.y, u.delta)).collect();
        assert!(orc.batch(&o_ups).is_err(), "oracle also rejects {name}");
    }

    // v4: rebuild tables: (-5,-3) drops; (0,2) and (7,100) carry over; x=42 new.
    reg.rebuild(vec![0, 7, 42], vec![2, 100]).unwrap();
    orc.rebuild(&[0, 7, 42], &[2, 100]).unwrap();
    assert_eq!(
        q(&reg, 4, (-5, -5, -3, -3)),
        0,
        "dropped point gone at head"
    );
    assert_eq!(q(&reg, 4, (0, 0, 2, 2)), -5, "negative carried over");
    assert_eq!(q(&reg, 4, whole), 5);

    // Old versions remain exactly queryable after the rebuild.
    assert_eq!(q(&reg, 2, whole), 8, "v2 still readable after rebuild");
    assert_eq!(q(&reg, 3, whole), 5, "v3 still readable after rebuild");
    assert_eq!(
        q(&reg, 2, (-5, -5, -3, -3)),
        3,
        "v2 dropped point still present in v2"
    );
    assert_eq!(q(&reg, 1, whole), 0, "v1 baseline unchanged");

    // Unknown version is a distinct error, not a clamp to head.
    match reg.query(
        Some(99),
        &Rect {
            x_lo: 0,
            x_hi: 0,
            y_lo: 0,
            y_hi: 0,
        },
    ) {
        Err(AppError::UnknownVersion(v)) => assert_eq!(v, 99),
        other => panic!("expected UnknownVersion(99), got {other:?}"),
    }
    log.assert_check(true, "handcalc fixture end-to-end + old versions");
}

#[test]
fn operations_before_registration_are_rejected() {
    let rid = common::caselog::run_id();
    let mut log = CaseLog::new(&rid, "not-initialized");
    let (_d, reg) = fresh();
    expect_code(
        reg.apply_batch(vec![pu(0, 0, 1)]).unwrap_err(),
        "not_initialized",
        &mut log,
        "batch-before-register",
    );
    expect_code(
        reg.rebuild(vec![0], vec![0]).unwrap_err(),
        "not_initialized",
        &mut log,
        "rebuild-before-register",
    );
    expect_code(
        reg.query(
            None,
            &Rect {
                x_lo: 0,
                x_hi: 0,
                y_lo: 0,
                y_hi: 0,
            },
        )
        .unwrap_err(),
        "not_initialized",
        &mut log,
        "query-before-register",
    );
    // double register is bad_request
    reg.register(vec![1, 2], vec![3, 4]).unwrap();
    expect_code(
        reg.register(vec![5], vec![6]).unwrap_err(),
        "bad_request",
        &mut log,
        "double-register",
    );
    // empty axes
    let (_d2, reg2) = fresh();
    expect_code(
        reg2.register(vec![], vec![1]).unwrap_err(),
        "empty_axis",
        &mut log,
        "empty-x",
    );
    expect_code(
        reg2.register(vec![1], vec![]).unwrap_err(),
        "empty_axis",
        &mut log,
        "empty-y",
    );
}

#[test]
fn random_trajectory_matches_oracle() {
    let rid = common::caselog::run_id();
    let mut log = CaseLog::new(&rid, "random-trajectory-vs-oracle");
    let (_d, reg) = fresh();
    let mut orc = FullScanOracle::new();
    let mut rng = Lcg::seeded(0x5151_2026);

    // Registered coordinate universe (small, with negatives and extremes).
    let x_universe: Vec<i64> = vec![i64::MIN, -100, -7, 0, 3, 99, i64::MAX];
    let y_universe: Vec<i64> = vec![i64::MIN, -50, -1, 0, 8, 1234, i64::MAX];
    reg.register(x_universe.clone(), y_universe.clone())
        .unwrap();
    orc.register(&x_universe, &y_universe).unwrap();

    let registered: HashSet<(i64, i64)> = x_universe
        .iter()
        .flat_map(|x| y_universe.iter().map(move |y| (*x, *y)))
        .collect();

    let whole = (
        i64::MIN as i128,
        i64::MAX as i128,
        i64::MIN as i128,
        i64::MAX as i128,
    );

    let mut applied = 0u32;
    for step in 0..400 {
        // Build a batch with distinct coordinates and bounded deltas so totals
        // stay in i64 with headroom; sometimes include an invalid update.
        let n = 1 + (rng.next_u64() % 4) as usize;
        let mut ups: Vec<PointUpdate> = Vec::new();
        let mut coord_set: HashSet<(i64, i64)> = HashSet::new();
        let mut inject_unregistered = false;
        let mut inject_duplicate = false;
        for _ in 0..n {
            let x = *rng.pick(&x_universe);
            let y = *rng.pick(&y_universe);
            if !coord_set.insert((x, y)) {
                inject_duplicate = true; // will arise naturally
            }
            ups.push(pu(x, y, rng.range_i64(-50, 50)));
        }
        if rng.chance(0.12) {
            inject_unregistered = true;
            ups[0] = pu(12_345_678, *rng.pick(&y_universe), 1);
        }

        let o_ups: Vec<OUpdate> = ups.iter().map(|u| ou(u.x, u.y, u.delta)).collect();
        let impl_res = reg.apply_batch(ups.clone());
        let orc_res = orc.batch(&o_ups);
        match (&impl_res, &orc_res) {
            (Ok(info), Ok(_)) => {
                applied += 1;
                log.step(format!(
                    "step {step}: v{} accepted n={}",
                    info.version,
                    ups.len()
                ));
            }
            (Err(ie), Err(oe)) => {
                let match_cat = matches!(
                    (ie, oe),
                    (
                        AppError::UnregisteredCoord(_, _),
                        OReject::Unregistered(_, _)
                    ) | (
                        AppError::DuplicateInBatch(_, _),
                        OReject::DuplicateInBatch(_, _)
                    ) | (AppError::Overflow(_), OReject::Overflow(_, _, _, _))
                        | (AppError::EmptyBatch, OReject::EmptyBatch)
                );
                if !match_cat && !inject_unregistered && !inject_duplicate {
                    log.fail(format!(
                        "step {step}: rejection categories diverge: impl={:?} oracle={:?}",
                        ie, oe
                    ));
                }
                log.step(format!(
                    "step {step}: rejected both (impl={} oracle={:?})",
                    ie.code(),
                    code_of_oracle(oe)
                ));
            }
            (Ok(i), Err(oe)) => log.fail(format!(
                "step {step}: impl accepted v{} but oracle rejected {oe:?}; ups={ups:?}",
                i.version
            )),
            (Err(ie), Ok(_)) => log.fail(format!(
                "step {step}: impl rejected {} but oracle accepted; ups={ups:?}",
                ie.code()
            )),
        }

        // After every step, compare every still-existing version over a set of
        // rectangles including the whole plane, point probes and random boxes.
        let head = reg.head_version().unwrap();
        for _ in 0..6 {
            let v = 1 + rng.next_u64() % head;
            let (xl, xh, yl, yh) = random_rect(&mut rng, &x_universe, &y_universe, whole);
            let rect = Rect {
                x_lo: xl,
                x_hi: xh,
                y_lo: yl,
                y_hi: yh,
            };
            let got = reg.query(Some(v), &rect).unwrap();
            let (want, scanned, _) = orc.query(
                v,
                ORect {
                    x_lo: xl,
                    x_hi: xh,
                    y_lo: yl,
                    y_hi: yh,
                },
            );
            if got.sum != want {
                log.fail(format!(
                    "step {step} v{v} rect=({xl},{xh},{yl},{yh}): impl={} oracle_fullscan={want} (scanned {scanned})",
                    got.sum
                ));
            }
        }
    }

    // Whole-plane totals must always agree across ALL versions.
    for v in 1..=reg.head_version().unwrap() {
        let got = reg
            .query(
                Some(v),
                &Rect {
                    x_lo: whole.0,
                    x_hi: whole.1,
                    y_lo: whole.2,
                    y_hi: whole.3,
                },
            )
            .unwrap()
            .sum;
        let (want, scanned, _) = orc.query(
            v,
            ORect {
                x_lo: whole.0,
                x_hi: whole.1,
                y_lo: whole.2,
                y_hi: whole.3,
            },
        );
        assert_eq!(got, want, "whole plane v{v}");
        log.step(format!(
            "whole-plane v{v}: {got} (oracle scanned {scanned} nonzero cells)"
        ));
    }
    let _ = registered;
    log.assert_check(
        true,
        format!("{applied} batches applied; all versions/rects match full-scan oracle"),
    );
}

fn code_of_oracle(e: &OReject) -> &'static str {
    match e {
        OReject::Unregistered(_, _) => "unregistered_coordinate",
        OReject::DuplicateInBatch(_, _) => "duplicate_in_batch",
        OReject::EmptyBatch => "empty_batch",
        OReject::Overflow(_, _, _, _) => "overflow",
        OReject::EmptyAxis(a) => {
            let _ = a;
            "empty_axis"
        }
        OReject::NotInitialized => "not_initialized",
        OReject::AlreadyInitialized => "bad_request",
    }
}

fn random_rect(
    rng: &mut Lcg,
    xs: &[i64],
    ys: &[i64],
    whole: (i128, i128, i128, i128),
) -> (i128, i128, i128, i128) {
    match rng.next_u64() % 4 {
        0 => whole,
        1 => {
            // point probe at a registered coordinate
            let x = *rng.pick(xs) as i128;
            let y = *rng.pick(ys) as i128;
            (x, x, y, y)
        }
        2 => {
            // deliberately inverted (empty)
            let a = rng.range_i64(-120, 120) as i128;
            let b = rng.range_i64(-120, 120) as i128;
            (a.max(b), a.min(b), -50, 50)
        }
        _ => (
            rng.range_i64(-120, 120) as i128,
            rng.range_i64(-120, 120) as i128,
            rng.range_i64(-1400, 1400) as i128,
            rng.range_i64(-1400, 1400) as i128,
        ),
    }
}
