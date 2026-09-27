#![allow(clippy::type_complexity)]
//! Kernel tests: coordinate compression + 2D Fenwick.
//!
//! Expected answers come either from hand-computed literals or from a local
//! independent HashMap full scan written inside this file — never from the
//! Fenwick itself.

mod common;

use common::rng::Lcg;
use prereg2d::index::compress::CoordTable;
use prereg2d::index::fenwick::{Fenwick2D, RectQuery};
use prereg2d::index::grid::DenseGrid;
use std::collections::HashMap;

type Pt = (i64, i64);

/// Independent full scan over a (coords, sparse totals) model.
fn scan(totals: &HashMap<Pt, i64>, r: (i128, i128, i128, i128)) -> i128 {
    let (xl, xh, yl, yh) = r;
    if xl > xh || yl > yh {
        return 0;
    }
    totals
        .iter()
        .filter(|((x, y), _)| {
            let (x, y) = (*x as i128, *y as i128);
            xl <= x && x <= xh && yl <= y && y <= yh
        })
        .map(|(_, v)| *v as i128)
        .sum()
}

/// Build dense grid from an independent sparse map.
fn grid_from(table: &CoordTable, totals: &HashMap<Pt, i64>) -> DenseGrid {
    let (nx, ny) = table.dims();
    let mut g = DenseGrid::zeros(nx, ny);
    for ((x, y), v) in totals {
        let (ix, iy) = table
            .index_of(*x, *y)
            .expect("totals point must be registered");
        g.set(ix, iy, *v);
    }
    g
}

#[test]
fn compression_order_is_fixed_and_deduped() {
    let rid = common::caselog::run_id();
    let mut log = common::caselog::CaseLog::new(&rid, "compression-fixed-order");
    let t = CoordTable::new(vec![7, -5, 0, 7, 0], vec![100, -3, 2, -3]);
    log.step(format!("xs={:?}", t.xs.coords()));
    log.step(format!("ys={:?}", t.ys.coords()));
    log.assert_check(t.xs.coords() == [-5, 0, 7], "x sorted + dedup");
    log.assert_check(t.ys.coords() == [-3, 2, 100], "y sorted + dedup");
    log.assert_check(
        t.xs.rank_of(-5) == Some(1) && t.xs.rank_of(7) == Some(3),
        "1-based ranks",
    );
    log.assert_check(t.xs.rank_of(6).is_none(), "absent coord has no rank");
    // prefix cutoffs at interesting boundaries
    log.assert_check(t.xs.rank_prefix(6) == 2, "rank_prefix(6)=2 (-5,0)");
    log.assert_check(t.xs.rank_prefix(7) == 3, "rank_prefix(7)=3 inclusive");
    log.assert_check(t.xs.rank_prefix(-1_000_000) == 0, "rank_prefix(below)=0");
    log.assert_check(t.xs.rank_prefix(1_000_000) == 3, "rank_prefix(above)=3");
}

#[test]
fn hand_values_extreme_coords_and_empty_rects() {
    let rid = common::caselog::run_id();
    let mut log = common::caselog::CaseLog::new(&rid, "kernel-hand-extremes");

    let xs = vec![i64::MIN, -1, 0, 1, i64::MAX];
    let ys = vec![i64::MIN, -1, 0, 1, i64::MAX];
    let table = CoordTable::new(xs.clone(), ys.clone());
    let mut totals: HashMap<Pt, i64> = HashMap::new();
    // Hand-placed weights, including negatives.
    totals.insert((i64::MIN, i64::MIN), -17);
    totals.insert((-1, -1), 4);
    totals.insert((0, 0), -5);
    totals.insert((1, 1), 6);
    totals.insert((i64::MAX, i64::MAX), 100);
    totals.insert((i64::MIN, i64::MAX), 7);
    let grid = grid_from(&table, &totals);
    let fw = Fenwick2D::from_grid(&grid);
    let sum = |r: (i128, i128, i128, i128)| RectQuery::new(&fw, &table, r.0, r.1, r.2, r.3).sum();

    let min = i64::MIN as i128;
    let max = i64::MAX as i128;

    let cases: &[(&str, (i128, i128, i128, i128), i128)] = &[
        ("whole plane at coord extrema", (min, max, min, max), 95), // -17+4-5+6+100+7
        ("corner min,min", (min, min, min, min), -17),
        ("corner max,max", (max, max, max, max), 100),
        ("cross corners min-x row", (min, min, min, max), -10), // -17 + 7
        ("origin cell", (0, 0, 0, 0), -5),
        ("inclusive window [-1,1]", (-1, 1, -1, 1), 5), // 4-5+6
        ("gap with no coords", (2, max - 1, 2, max - 1), 0),
        ("empty inverted x", (1, 0, min, max), 0),
        ("empty inverted y", (min, max, 1, 0), 0),
        (
            "bounds wider than i64",
            (i128::MIN, i128::MAX, i128::MIN, i128::MAX),
            95,
        ),
        ("just below min excluded", (i128::MIN, min - 1, min, max), 0),
    ];
    for (name, r, want) in cases {
        let got = sum(*r);
        let scan_got = scan(&totals, *r);
        log.step(format!(
            "{name}: fenwick={got} fullscan={scan_got} want={want}"
        ));
        log.assert_check(got == *want, format!("{name}: {got} == {want}"));
        log.assert_check(
            got == scan_got,
            format!("{name}: matches independent full scan"),
        );
    }
}

#[test]
fn fenwick_matches_full_scan_on_random_grids_and_rects() {
    let rid = common::caselog::run_id();
    let mut log = common::caselog::CaseLog::new(&rid, "kernel-random-vs-fullscan");
    let mut rng = Lcg::seeded(20260928);

    for iter in 0..300 {
        // Small distinct coordinate sets built from a bounded pool so weights
        // and rectangle sums stay comfortably inside i128.
        let pool_x: Vec<i64> = (-20..=20).step_by(3).collect();
        let pool_y: Vec<i64> = (-20..=20).step_by(2).collect();
        let nx = 1 + (rng.next_u64() as usize % pool_x.len());
        let ny = 1 + (rng.next_u64() as usize % pool_y.len());
        let xs: Vec<i64> = pool_x[..nx].to_vec();
        let ys: Vec<i64> = pool_y[..ny].to_vec();
        let table = CoordTable::new(xs.clone(), ys.clone());

        let mut totals: HashMap<Pt, i64> = HashMap::new();
        for &x in &xs {
            for &y in &ys {
                if rng.chance(0.55) {
                    totals.insert((x, y), rng.range_i64(-1_000, 1_000));
                }
            }
        }
        let grid = grid_from(&table, &totals);
        let fw = Fenwick2D::from_grid(&grid);

        for _ in 0..12 {
            // bounds range slightly outside the coordinate pool
            let xl = rng.range_i64(-25, 24) as i128;
            let xh = rng.range_i64(-24, 25) as i128;
            let yl = rng.range_i64(-25, 24) as i128;
            let yh = rng.range_i64(-24, 25) as i128;
            let got = RectQuery::new(&fw, &table, xl, xh, yl, yh).sum();
            let want = scan(&totals, (xl, xh, yl, yh));
            if got != want {
                log.step(format!("iter {iter}: xs={xs:?} ys={ys:?}"));
                log.step(format!("iter {iter}: rect=({xl},{xh},{yl},{yh})"));
                log.step(format!("iter {iter}: totals={:?}", totals));
                log.fail(format!("fenwick {got} != full scan {want}"));
            }
        }
    }
    log.assert_check(true, "300 random grids x 12 rects matched the full scan");
}

#[test]
fn fenwick_explain_exposes_steps_and_terms() {
    let rid = common::caselog::run_id();
    let mut log = common::caselog::CaseLog::new(&rid, "kernel-explain-steps");
    let table = CoordTable::new(vec![0, 1, 2], vec![0, 1, 2]);
    let mut totals: HashMap<Pt, i64> = HashMap::new();
    totals.insert((0, 0), 3);
    totals.insert((2, 2), 4);
    let grid = grid_from(&table, &totals);
    let fw = Fenwick2D::from_grid(&grid);

    // Rectangle [1,2]x[1,2] contains only (2,2)=4. Inclusion-exclusion:
    // hh=7 (0,0 & 2,2 prefixes), lh=3 (x<=0), hl=3 (y<=0), ll=3 (both <=0).
    let ex = RectQuery::new(&fw, &table, 1, 2, 1, 2).explain();
    log.step(format!("cutoffs={:?}", ex.cutoffs));
    log.step(format!("terms={:?} sum={}", ex.terms, ex.sum));
    log.assert_check(ex.sum == 4, "window sum 4");
    log.assert_check(
        ex.terms.hh == 7 && ex.terms.lh == 3 && ex.terms.hl == 3 && ex.terms.ll == 3,
        "the four prefix terms justify 7-3-3+3=4",
    );

    let empty_ex = RectQuery::new(&fw, &table, 2, 1, 0, 2).explain();
    log.assert_check(
        empty_ex.empty && empty_ex.sum == 0,
        "inverted x reported empty -> 0",
    );
}
