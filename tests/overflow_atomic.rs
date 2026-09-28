//! 负更新允许，但逐点累计溢出必须可检测并整批拒绝；
//! 批更新原子发布：校验失败时查询见不到任何半批效果，版本不前进。
//! 另含陈旧 base 版本冲突（STALE_BASE_VERSION）与空批。

mod common;

use common::{TempDir, TestLog};
use pr2d::model::PointUpdate;
use pr2d::rect::Rect;
use pr2d::store::Store;
use serde_json::json;

fn p(x: i64, y: i64, d: i64) -> PointUpdate {
    PointUpdate { x, y, delta: d }
}
fn rect_all() -> Rect {
    Rect {
        x_lo: i64::MIN,
        x_hi: i64::MAX,
        y_lo: i64::MIN,
        y_hi: i64::MAX,
    }
}

#[test]
fn point_overflow_rejects_entire_batch_and_keeps_version() {
    let tmp = TempDir::new("over-point");
    let store = Store::open(tmp.path()).unwrap();
    let mut log = TestLog::new("point_overflow");
    let t = store
        .register_table(vec![1, 2, 3], vec![10])
        .unwrap()
        .table_id;

    // v1: (1,10)=MAX
    store
        .commit_batch(t, None, vec![p(1, 10, i64::MAX)])
        .unwrap();

    // v2 试图 +1 → MAX+1 溢出；422 POINT_OVERFLOW，错误携带 previous/batch_delta
    let e = store.commit_batch(t, None, vec![p(1, 10, 1)]).unwrap_err();
    log.assert_eq_json(
        "positive-overflow",
        json!({"point":[1,10],"previous":i64::MAX,"delta":1}),
        json!({"code":"POINT_OVERFLOW","http":422}),
        json!({"code":e.code(),"http":e.http_status()}),
    );
    match &e {
        pr2d::error::CoreError::PointOverflow {
            x,
            y,
            previous,
            batch_delta,
        } => {
            assert_eq!((*x, *y, *previous, *batch_delta), (1, 10, i64::MAX, 1));
        }
        other => panic!("wrong error variant: {other:?}"),
    }

    // 负向溢出：MIN-1
    let e = store
        .commit_batch(t, None, vec![p(1, 10, 1), p(2, 10, i64::MIN), p(2, 10, -1)])
        .unwrap_err();
    assert_eq!(e.code(), "POINT_OVERFLOW");

    // 无新版本产生；(1,10) 仍是 MAX（第二次失败批次里的 +1 也没生效）
    let q = store
        .query(
            t,
            None,
            &Rect {
                x_lo: 1,
                x_hi: 1,
                y_lo: 10,
                y_hi: 10,
            },
        )
        .unwrap();
    log.assert_eq_json(
        "unchanged-after-reject",
        json!({"point":[1,10]}),
        json!({"version":1,"sum":i64::MAX}),
        json!({"version":q.version,"sum":q.sum}),
    );
    let q2 = store
        .query(
            t,
            None,
            &Rect {
                x_lo: 2,
                x_hi: 2,
                y_lo: 10,
                y_hi: 10,
            },
        )
        .unwrap();
    assert_eq!((q2.version, q2.sum), (1, 0));
}

#[test]
fn valid_negative_path_down_to_min() {
    // 负更新本身完全允许：MAX 累加负值回到 MIN 边界（精确等于不溢出）。
    let tmp = TempDir::new("over-neg-ok");
    let store = Store::open(tmp.path()).unwrap();
    let t = store.register_table(vec![1], vec![1]).unwrap().table_id;

    store
        .commit_batch(t, None, vec![p(1, 1, i64::MAX)])
        .unwrap();
    store
        .commit_batch(t, None, vec![p(1, 1, -i64::MAX)])
        .unwrap(); // MAX-MAX=0
    store
        .commit_batch(t, None, vec![p(1, 1, i64::MIN)])
        .unwrap(); // 到达 MIN
    let q = store
        .query(
            t,
            None,
            &Rect {
                x_lo: 1,
                x_hi: 1,
                y_lo: 1,
                y_hi: 1,
            },
        )
        .unwrap();
    assert_eq!(q.sum, i64::MIN);

    // 批内同点先聚合：分量 MAX+MAX+(-MAX)+(-MAX)=0，合法且无变化
    // （若不先聚合而顺序累加，中间 MAX+MIN 会误判——这正是“批内聚合”语义）。
    let v = store
        .commit_batch(
            t,
            None,
            vec![
                p(1, 1, i64::MAX),
                p(1, 1, i64::MAX),
                p(1, 1, -i64::MAX),
                p(1, 1, -i64::MAX),
            ],
        )
        .unwrap();
    let q = store
        .query(
            t,
            Some(v.version),
            &Rect {
                x_lo: 1,
                x_hi: 1,
                y_lo: 1,
                y_hi: 1,
            },
        )
        .unwrap();
    assert_eq!(q.sum, i64::MIN);
}

#[test]
fn sum_overflow_detected_at_query() {
    // 点值各自合法，但矩形和超过 i64 → 查询 SUM_OVERFLOW（422）。
    let tmp = TempDir::new("over-sum");
    let store = Store::open(tmp.path()).unwrap();
    let mut log = TestLog::new("sum_overflow");
    let t = store.register_table(vec![1, 2], vec![1]).unwrap().table_id;
    store
        .commit_batch(t, None, vec![p(1, 1, i64::MAX), p(2, 1, i64::MAX)])
        .unwrap();

    let single = store
        .query(
            t,
            None,
            &Rect {
                x_lo: 1,
                x_hi: 1,
                y_lo: 1,
                y_hi: 1,
            },
        )
        .unwrap();
    assert_eq!(single.sum, i64::MAX);
    let e = store.query(t, None, &rect_all()).unwrap_err();
    log.assert_eq_json(
        "sum-overflow",
        json!({"rect":"all","points":[i64::MAX,i64::MAX]}),
        json!({"code":"SUM_OVERFLOW","http":422}),
        json!({"code":e.code(),"http":e.http_status()}),
    );
}

#[test]
fn stale_base_version_conflict_and_empty_batch() {
    let tmp = TempDir::new("stale");
    let store = Store::open(tmp.path()).unwrap();
    let mut log = TestLog::new("stale_base");
    let t = store.register_table(vec![1, 2], vec![10]).unwrap().table_id;

    let v1 = store.commit_batch(t, None, vec![p(1, 10, 1)]).unwrap();
    assert_eq!(v1.version, 1);

    // 基于陈旧 v0 提交 → 409 STALE_BASE_VERSION
    let e = store
        .commit_batch(t, Some(0), vec![p(1, 10, 2)])
        .unwrap_err();
    log.assert_eq_json(
        "stale",
        json!({"base_version":0,"current":1}),
        json!({"code":"STALE_BASE_VERSION","http":409}),
        json!({"code":e.code(),"http":e.http_status()}),
    );

    // 显式基于当前版本提交成功（链式）
    let v2 = store.commit_batch(t, Some(1), vec![p(2, 10, 5)]).unwrap();
    assert_eq!(v2.version, 2);
    assert_eq!(v2.base_version, 1);

    // 不存在的版本号 → 404 VERSION_NOT_FOUND
    let e = store
        .commit_batch(t, Some(99), vec![p(1, 10, 1)])
        .unwrap_err();
    assert_eq!(e.code(), "VERSION_NOT_FOUND");
    let e = store.query(t, Some(99), &rect_all()).unwrap_err();
    assert_eq!(e.code(), "VERSION_NOT_FOUND");

    // 空批 → 400 EMPTY_BATCH
    let e = store.commit_batch(t, None, vec![]).unwrap_err();
    assert_eq!(e.code(), "EMPTY_BATCH");

    // 版本链未受失败操作影响
    let infos = store.with_table(t, |tb| tb.versions()).unwrap();
    log.assert_eq_json(
        "versions",
        json!({}),
        json!({"latest":2,"count":3}),
        json!({"latest":infos.last().unwrap().version, "count":infos.len()}),
    );
}
