//! 坐标重新建表：新表使用不同压缩表（顺序/成员均可变化），
//! 旧表的历史版本仍可查询；并验证重启进程（重放 WAL）后旧版本依然可读。

mod common;

use common::{TempDir, TestLog};
use pr2d::model::PointUpdate;
use pr2d::rect::Rect;
use pr2d::store::Store;
use serde_json::json;

fn p(x: i64, y: i64, d: i64) -> PointUpdate {
    PointUpdate { x, y, delta: d }
}

#[test]
fn rebuild_coordinates_old_versions_remain_queryable() {
    let tmp = TempDir::new("rebuild");
    let store = Store::open(tmp.path()).unwrap();
    let mut log = TestLog::new("rebuild_coords");

    // 旧表：xs=[10,20]（顺序故意打乱+重复）
    let t1 = store
        .register_table(vec![20, 10, 10], vec![5])
        .unwrap()
        .table_id;
    store
        .commit_batch(t1, None, vec![p(10, 5, 100), p(20, 5, 200)])
        .unwrap();
    store.commit_batch(t1, None, vec![p(10, 5, -30)]).unwrap();
    // t1: v2 → (10,5)=70, (20,5)=200

    // 重新建表：全新坐标（含 15 这个旧表没有的坐标；不含 5）
    let t2 = store
        .register_table(vec![10, 15, 20, 30], vec![5, 9])
        .unwrap()
        .table_id;
    assert_ne!(t1, t2);
    store
        .commit_batch(t2, None, vec![p(15, 9, 7), p(30, 5, -2)])
        .unwrap();

    // 旧表的每个历史版本都还在，且新表不影响其结果
    let old_cases: Vec<(u64, i64)> = vec![(0, 0), (1, 300), (2, 270)];
    let full = Rect {
        x_lo: i64::MIN,
        x_hi: i64::MAX,
        y_lo: i64::MIN,
        y_hi: i64::MAX,
    };
    for (v, want) in &old_cases {
        let q = store.query(t1, Some(*v), &full).unwrap();
        log.assert_eq_json(
            "t1-history",
            json!({"table":t1,"version":v}),
            json!({"sum":want}),
            json!({"sum":q.sum}),
        );
    }
    // 新表当前值
    let q = store.query(t2, None, &full).unwrap();
    log.assert_eq_json(
        "t2-latest",
        json!({"table":t2}),
        json!({"sum":5,"version":1}),
        json!({"sum":q.sum,"version":q.version}),
    );
    // 新表的点 (15,9) 在旧坐标系里天然不存在；对旧表查询它 → 空矩形
    let q = store
        .query(
            t1,
            None,
            &Rect {
                x_lo: 15,
                x_hi: 15,
                y_lo: 9,
                y_hi: 9,
            },
        )
        .unwrap();
    assert_eq!((q.sum, q.empty), (0, true));

    // 重启：用同一数据目录新打开一个 Store，重放 WAL
    drop(store);
    let store2 = Store::open(tmp.path()).unwrap();
    for (v, want) in old_cases {
        let q = store2.query(t1, Some(v), &full).unwrap();
        log.assert_eq_json(
            "t1-after-restart",
            json!({"table":t1,"version":v}),
            json!({"sum":want}),
            json!({"sum":q.sum}),
        );
    }
    let q = store2.query(t2, None, &full).unwrap();
    assert_eq!((q.sum, q.version), (5, 1));
    // 重放后继续提交，版本号必须接着 v2 → v3
    let v3 = store2.commit_batch(t1, None, vec![p(20, 5, 1)]).unwrap();
    assert_eq!(v3.version, 3);
    let q = store2.query(t1, Some(3), &full).unwrap();
    assert_eq!(q.sum, 271);
}
