//! 随机交叉验证：固定种子可复现。
//! 内核结果必须在每个版本、每个随机矩形上与独立稀疏映射全扫描 Oracle 一致。
//! 失败时 TestLog 记录完整坐标、全部批次序列与差异矩形，可据此复现。

mod common;

use common::{Oracle, OracleRect, TempDir, TestLog};
use pr2d::model::PointUpdate;
use pr2d::rect::Rect;
use pr2d::store::Store;
use serde_json::json;

/// 确定性 LCG（不依赖 rand crate，固定版本行为）。
struct Rng(u64);

impl Rng {
    fn next_u64(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0
    }
    fn below(&mut self, n: u64) -> usize {
        (self.next_u64() % n) as usize
    }
    fn i64_in(&mut self, lo: i64, hi: i64) -> i64 {
        let span = (hi as i128 - lo as i128 + 1) as u64;
        lo + (self.next_u64() % span) as i64
    }
    fn delta(&mut self) -> i64 {
        // 大部分是小增量，偶尔大值，保证路径多样化但避开必然溢出
        match self.below(10) {
            0 => self.i64_in(-1_000_000, 1_000_000),
            1 => *[i64::MAX / 4, i64::MIN / 4, 1, -1]
                .get(self.below(4))
                .unwrap(),
            _ => self.i64_in(-50, 50),
        }
    }
}

#[test]
fn randomized_matches_sparse_scan_oracle() {
    // 多组不同形状/种子，规模保持小以便失败时可人工核对。
    let shapes = [
        (vec![-5, 0, 5, 9], vec![-2, 2, 8]),
        (vec![1], vec![1, 2]),
        (vec![-100, 100], vec![-100, 100]),
        (vec![7, 1, 4, 4, 10, -3], vec![2, 2, 9]),
    ];

    for (seed, (xs, ys)) in shapes.iter().enumerate() {
        run_one(seed as u64, xs.clone(), ys.clone());
    }
}

fn run_one(seed: u64, xs: Vec<i64>, ys: Vec<i64>) {
    let tmp = TempDir::new(&format!("xcheck-{seed}"));
    let store = Store::open(tmp.path()).unwrap();
    let mut rng = Rng(0x9E37_79B9_7F4A_7C15u64 ^ seed.wrapping_mul(0xDEAD_BEEF));
    let mut log = TestLog::new(&format!("crosscheck_{seed}"));

    let t = store
        .register_table(xs.clone(), ys.clone())
        .unwrap()
        .table_id;
    let mut oracle = Oracle::new(xs.clone(), ys.clone()).unwrap();

    let n_batches = 12;
    for b in 0..n_batches {
        // 随机批次（含同点重复）
        let n_updates = 1 + rng.below(6);
        let mut updates = Vec::new();
        for _ in 0..n_updates {
            let x = xs[rng.below(xs.len() as u64)];
            let y = ys[rng.below(ys.len() as u64)];
            updates.push(PointUpdate {
                x,
                y,
                delta: rng.delta(),
            });
        }

        // 两边独立判定（溢出/拒绝类别也必须一致）
        let core_res = store.commit_batch(t, None, updates.clone());
        let ora_res = oracle.commit(&updates);
        match (&core_res, &ora_res) {
            (Ok(c), Ok(o)) => {
                assert_eq!(c.version, *o, "version mismatch seed={seed} batch={b}");
            }
            (Err(ce), Err(oe)) => {
                assert_eq!(
                    ce.code(),
                    oe.code,
                    "reject category mismatch seed={seed} batch={b}: core={} oracle={}",
                    ce.code(),
                    oe.code
                );
                // 拒绝的批次两边都不前进，继续下一批
                continue;
            }
            (c, o) => panic!("commit disagreement seed={seed} batch={b}: core={c:?} oracle={o:?}"),
        }

        // 对当前版本做一批矩形查询，全域 + 随机矩形（含缝隙与极端边界）
        let latest = oracle.latest_version();
        let mut rects: Vec<Rect> = vec![Rect {
            x_lo: i64::MIN,
            x_hi: i64::MAX,
            y_lo: i64::MIN,
            y_hi: i64::MAX,
        }];
        for _ in 0..10 {
            let mut xa = rng.i64_in(-12, 12);
            let mut xb = rng.i64_in(-12, 12);
            let mut ya = rng.i64_in(-4, 12);
            let mut yb = rng.i64_in(-4, 12);
            if xa > xb {
                std::mem::swap(&mut xa, &mut xb);
            }
            if ya > yb {
                std::mem::swap(&mut ya, &mut yb);
            }
            rects.push(Rect {
                x_lo: xa,
                x_hi: xb,
                y_lo: ya,
                y_hi: yb,
            });
        }
        // 偶发倒矩形：类别必须一致
        if rng.below(3) == 0 {
            rects.push(Rect {
                x_lo: 100,
                x_hi: -100,
                y_lo: 0,
                y_hi: 1,
            });
        }

        for r in rects {
            let core_q = store.query(t, Some(latest), &r);
            let ora_q = oracle.query(
                latest,
                OracleRect {
                    x_lo: r.x_lo,
                    x_hi: r.x_hi,
                    y_lo: r.y_lo,
                    y_hi: r.y_hi,
                },
            );
            match (core_q, ora_q) {
                (Ok(cq), Ok((osum, ononempty))) => {
                    if osum < i64::MIN as i128 || osum > i64::MAX as i128 {
                        panic!(
                            "oracle sum {osum} exceeds i64 but core returned Ok at seed={seed} batch={b} rect={:?}",
                            (r.x_lo, r.x_hi, r.y_lo, r.y_hi)
                        );
                    }
                    log.assert_eq_json(
                        &format!("b{b}-query"),
                        json!({"seed":seed,"batch":b,"rect":[r.x_lo,r.x_hi,r.y_lo,r.y_hi],
                               "updates":updates.iter().map(|u| [u.x,u.y,u.delta]).collect::<Vec<_>>()}),
                        json!({"sum":osum,"empty":!ononempty}),
                        json!({"sum":cq.sum,"empty":cq.empty}),
                    );
                }
                (Err(ce), Err(oe)) => {
                    // 全域和溢出：两侧必须都报 SUM_OVERFLOW；倒矩形：两侧都报 INVERTED_RECT
                    assert_eq!(ce.code(), oe.code, "query reject mismatch seed={seed}");
                }
                (Err(ce), Ok((osum, _))) => {
                    if ce.code() == "SUM_OVERFLOW" {
                        assert!(
                            osum < i64::MIN as i128 || osum > i64::MAX as i128,
                            "core SUM_OVERFLOW but oracle fit ({osum}) at seed={seed} batch={b}"
                        );
                    } else {
                        panic!("core rejected {} but oracle Ok at seed={seed}", ce.code());
                    }
                }
                (Ok(cq), Err(oe)) => {
                    panic!(
                        "core Ok({:?}) but oracle rejected {} at seed={seed}",
                        cq.sum, oe.code
                    );
                }
            }
        }
    }
}
