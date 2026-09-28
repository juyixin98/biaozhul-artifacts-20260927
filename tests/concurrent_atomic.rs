//! 并发原子性：多线程同时提交批次。
//! 必须满足：
//! 1. 成功提交的版本号恰好构成 1..=N 无缺失（每个批次作为整体发布一次）；
//! 2. 任何时刻查询都看不到半批——每个已发布版本的全域和等于“截至该版本
//!    所有成功批次净增量之和”（用独立聚合账本来核对）；
//! 3. 所有成功批次的净增量总和守恒于最终全域和。

mod common;

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use common::{TempDir, TestLog};
use pr2d::model::PointUpdate;
use pr2d::rect::Rect;
use pr2d::store::Store;
use serde_json::json;

#[test]
fn concurrent_batches_are_atomic_and_versions_dense() {
    let tmp = TempDir::new("concurrent");
    let store = Arc::new(Store::open(tmp.path()).unwrap());
    let mut log = TestLog::new("concurrent_atomic");

    // 4 个注册点，网格小、便于手算守恒
    let t = store
        .register_table(vec![1, 2], vec![10, 20])
        .unwrap()
        .table_id;
    let all = Rect {
        x_lo: i64::MIN,
        x_hi: i64::MAX,
        y_lo: i64::MIN,
        y_hi: i64::MAX,
    };

    let n_threads = 8usize;
    let per_thread = 40usize;

    // 每批两点、同批净增量固定为 -7：(1,10)+3, (2,20)-10。
    // 多批累计可能使某点跌破 i64::MIN 吗？总批数 320 → (2,20) 累计 -3200，安全。
    let mk = || {
        vec![
            PointUpdate {
                x: 1,
                y: 10,
                delta: 3,
            },
            PointUpdate {
                x: 2,
                y: 20,
                delta: -10,
            },
        ]
    };

    let mut handles = Vec::new();
    for _ in 0..n_threads {
        let store = Arc::clone(&store);
        handles.push(std::thread::spawn(move || {
            let mut committed = Vec::new();
            for _ in 0..per_thread {
                // 不带 base_version：提交点用“当前最新”乐观语义，冲突会 409。
                match store.commit_batch(t, None, mk()) {
                    Ok(out) => committed.push(out.version),
                    Err(e) => {
                        // 并发下唯一允许的失败类别是陈旧版本（本实现写锁串行化，
                        // 理论上极少出现；其他类别一律不允许）。
                        assert_eq!(e.code(), "STALE_BASE_VERSION", "unexpected error: {e}");
                    }
                }
            }
            committed
        }));
    }

    let mut all_committed: Vec<u64> = Vec::new();
    for h in handles {
        all_committed.extend(h.join().unwrap());
    }

    // (1) 版本号稠密无缺、无重复
    let unique: HashSet<u64> = all_committed.iter().copied().collect();
    let latest = all_committed.iter().copied().max().unwrap_or(0);
    let mut sorted: Vec<u64> = unique.iter().copied().collect();
    sorted.sort_unstable();
    let expect_dense: Vec<u64> = (1..=latest).collect();
    log.assert_eq_json(
        "dense-versions",
        json!({"threads":n_threads,"per_thread":per_thread}),
        json!({
            "committed": expect_dense.len(),
            "unique_versions": expect_dense,
        }),
        json!({
            "committed": all_committed.len(),
            "unique_versions": sorted,
        }),
    );
    assert_eq!(
        all_committed.len(),
        unique.len(),
        "a version was published twice"
    );
    for v in 1..=latest {
        assert!(unique.contains(&v), "missing version {v} — half/lost batch");
    }

    // (2) 每个已发布版本的全域和 == -7 * v（每成功批净增量 -7，且批次原子生效）。
    // 逐版本查询同时验证“时间旅行”在并发写入下仍然自洽。
    for v in 0..=latest {
        let q = store.query(t, Some(v), &all).unwrap();
        let want = -7i64 * v as i64;
        log.assert_eq_json(
            "per-version-total",
            json!({"version":v}),
            json!({"sum":want}),
            json!({"sum":q.sum}),
        );
    }

    // (3) 逐点守恒账本（独立于 BIT，直接按版本数乘）
    let pt = |x, y| {
        store
            .query(
                t,
                Some(latest),
                &Rect {
                    x_lo: x,
                    x_hi: x,
                    y_lo: y,
                    y_hi: y,
                },
            )
            .unwrap()
            .sum
    };
    let ledger: HashMap<(i64, i64), i64> =
        HashMap::from([((1, 10), 3 * latest as i64), ((2, 20), -10 * latest as i64)]);
    log.assert_eq_json(
        "point-ledger",
        json!({"committed_batches":latest}),
        json!({"(1,10)":ledger[&(1,10)],"(2,20)":ledger[&(2,20)]}),
        json!({"(1,10)":pt(1,10),"(2,20)":pt(2,20)}),
    );

    // 重启后版本与守恒仍然成立（WAL 重放并发写入结果）
    drop(store);
    let store2 = Store::open(tmp.path()).unwrap();
    let q = store2.query(t, Some(latest), &all).unwrap();
    assert_eq!(q.sum, -7 * latest as i64);
}
