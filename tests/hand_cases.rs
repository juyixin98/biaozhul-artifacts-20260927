//! 手算场景：期望值全部在测试源码中以常量手算写出，
//! 同时与独立稀疏映射全扫描参考实现（`common::Oracle`）三方比对：
//!   手算常量 == Oracle 全扫描 == pr2d 内核结果。
//!
//! 覆盖题目点名的输入：重复坐标、负权、空矩形、极端坐标（i64::MIN/MAX）、
//! 未注册拒绝、负版本/倒矩形等失败类别、历史版本时间旅行。

mod common;

use common::{Oracle, OracleRect, TempDir, TestLog};
use pr2d::model::PointUpdate;
use pr2d::rect::Rect;
use pr2d::store::Store;
use serde_json::json;

fn p(x: i64, y: i64, d: i64) -> PointUpdate {
    PointUpdate { x, y, delta: d }
}

fn rect(x_lo: i64, x_hi: i64, y_lo: i64, y_hi: i64) -> Rect {
    Rect {
        x_lo,
        x_hi,
        y_lo,
        y_hi,
    }
}

/// 场景 A：重复注册坐标、同点增量累加（含负权）、闭区间边界。
#[test]
fn case_a_duplicate_coords_and_cumulative_points() {
    let tmp = TempDir::new("hand-a");
    let store = Store::open(tmp.path()).unwrap();
    let mut log = TestLog::new("case_a");

    // 注册 xs=[1,2,3,3]（1 个重复）、ys=[10,20,20]（1 个重复）→ 3×2 冻结网格
    let reg = store
        .register_table(vec![1, 2, 3, 3], vec![10, 20, 20])
        .unwrap();
    log.assert_eq_json(
        "register",
        json!({"xs":[1,2,3,3],"ys":[10,20,20]}),
        json!({"table_id":1,"version":0,"nx":3,"ny":2,"dup_x":1,"dup_y":1}),
        json!({"table_id":reg.table_id,"version":reg.version,"nx":reg.nx,"ny":reg.ny,
               "dup_x":reg.duplicate_x,"dup_y":reg.duplicate_y}),
    );
    let t = 1u32;

    // v1: (1,10)+10, (3,20)+5, (1,10)+2, (2,20)-3
    let v1 = store
        .commit_batch(
            t,
            None,
            vec![p(1, 10, 10), p(3, 20, 5), p(1, 10, 2), p(2, 20, -3)],
        )
        .unwrap();
    assert_eq!(v1.version, 1);

    let mut oracle = Oracle::new(vec![1, 2, 3, 3], vec![10, 20, 20]).unwrap();
    oracle
        .commit(&[p(1, 10, 10), p(3, 20, 5), p(1, 10, 2), p(2, 20, -3)])
        .unwrap();

    // 手算网格（行 x∈{1,2,3}，列 y∈{10,20}）：
    //   (1,10)=12  (1,20)=0
    //   (2,10)= 0  (2,20)=-3
    //   (3,10)= 0  (3,20)= 5
    let checks: Vec<(Rect, i64, bool)> = vec![
        // 单点（闭区间，上下界相同）
        (rect(1, 1, 10, 10), 12, false),
        (rect(3, 3, 20, 20), 5, false),
        (rect(2, 2, 20, 20), -3, false),
        // 整矩形：12 + (-3) + 5 = 14
        (rect(1, 3, 10, 20), 14, false),
        // 部分：x=1..2 两列全 y → 12 + (-3) = 9
        (rect(1, 2, 10, 20), 9, false),
        // 边界夹在坐标之间：[1,2]×[11,19] 不含任何已注册 y → 空矩形 0
        (rect(1, 2, 11, 19), 0, true),
        // 坐标缝隙：x∈(1,3) 开区间意义由闭界表达：[2,2]×[10,20] = -3
        (rect(2, 2, 10, 20), -3, false),
        // 无点矩形但格点存在（值为 0）：empty=false
        (rect(1, 3, 10, 10), 12, false),
        (rect(2, 2, 10, 10), 0, false),
    ];
    for (r, want_sum, want_empty) in checks {
        let got = store.query(t, Some(1), &r).unwrap();
        let (osum, ononempty) = oracle
            .query(
                1,
                OracleRect {
                    x_lo: r.x_lo,
                    x_hi: r.x_hi,
                    y_lo: r.y_lo,
                    y_hi: r.y_hi,
                },
            )
            .unwrap();
        log.assert_eq_json(
            "query-v1",
            json!({"rect":[r.x_lo,r.x_hi,r.y_lo,r.y_hi],"version":1}),
            json!({"sum":want_sum,"empty":want_empty,"oracle_sum":osum,"oracle_nonempty":ononempty}),
            json!({"sum":got.sum,"empty":got.empty,"oracle_sum":osum,"oracle_nonempty":ononempty}),
        );
    }

    // v2: (3,20)+8 → 13；(1,10)-7 → 5
    let v2 = store
        .commit_batch(t, None, vec![p(3, 20, 8), p(1, 10, -7)])
        .unwrap();
    assert_eq!(v2.version, 2);
    oracle.commit(&[p(3, 20, 8), p(1, 10, -7)]).unwrap();

    let got = store.query(t, Some(2), &rect(1, 3, 10, 20)).unwrap();
    log.assert_eq_json(
        "query-v2",
        json!({"rect":[1,3,10,20],"version":2}),
        json!({"sum":15}), // 5 + (-3) + 13
        json!({"sum":got.sum}),
    );
    // 历史版本时间旅行：v1 整矩形仍是 14，不被 v2 覆盖
    let old = store.query(t, Some(1), &rect(1, 3, 10, 20)).unwrap();
    log.assert_eq_json(
        "time-travel-v1",
        json!({"version":1}),
        json!({"sum":14}),
        json!({"sum":old.sum}),
    );
    // 基线版本 0 恒为 0
    let base = store.query(t, Some(0), &rect(1, 3, 10, 20)).unwrap();
    assert_eq!((base.sum, base.empty), (0, false));
}

/// 场景 B：负权主导 + 空矩形两类语义 + 极端坐标边界（i64::MIN/MAX，无算术溢出）。
#[test]
fn case_b_negative_weights_empty_rect_extremes() {
    let tmp = TempDir::new("hand-b");
    let store = Store::open(tmp.path()).unwrap();
    let mut log = TestLog::new("case_b");

    // 含极端坐标的注册
    let reg = store
        .register_table(vec![i64::MIN, 0, i64::MAX], vec![i64::MIN, i64::MAX])
        .unwrap();
    assert_eq!((reg.nx, reg.ny), (3, 2));
    let t = reg.table_id;

    // v1: (MIN,MIN)+100, (0,MAX)-250, (MAX,MIN)+3, (MAX,MAX)+7
    let ups = vec![
        p(i64::MIN, i64::MIN, 100),
        p(0, i64::MAX, -250),
        p(i64::MAX, i64::MIN, 3),
        p(i64::MAX, i64::MAX, 7),
    ];
    store.commit_batch(t, None, ups.clone()).unwrap();
    let mut oracle = Oracle::new(vec![i64::MIN, 0, i64::MAX], vec![i64::MIN, i64::MAX]).unwrap();
    oracle.commit(&ups).unwrap();

    let checks: Vec<(Rect, i64, bool)> = vec![
        // 全域
        (
            rect(i64::MIN, i64::MAX, i64::MIN, i64::MAX),
            100 - 250 + 3 + 7,
            false,
        ),
        // 只取负权点：(0,MAX)=-250；下界 x=MIN+1 夹在 MIN 与 0 之间
        (
            rect(
                i64::MIN.saturating_add(1),
                0,
                i64::MIN.saturating_add(1),
                i64::MAX,
            ),
            -250,
            false,
        ),
        // 空矩形：x 区间 (MIN,0) 之间，用闭界 [MIN+1, -1]
        (
            rect(i64::MIN.saturating_add(1), -1, i64::MIN, i64::MAX),
            0,
            true,
        ),
        // 空矩形：y 区间 (MIN,MAX) 之间
        (rect(i64::MIN, i64::MAX, 0, i64::MAX - 1), 0, true),
        // 边界精确落在极端坐标
        (rect(i64::MIN, i64::MIN, i64::MIN, i64::MIN), 100, false),
        (rect(i64::MAX, i64::MAX, i64::MAX, i64::MAX), 7, false),
        // 非空但净值为 0 的选择（构造：x=0 一列 y 全域，仅 -250，不为0；改用两点抵消）
        (rect(0, 0, i64::MIN, i64::MIN), 0, false),
    ];
    for (r, want_sum, want_empty) in checks {
        let got = store.query(t, Some(1), &r).unwrap();
        let (osum, ononempty) = oracle
            .query(
                1,
                OracleRect {
                    x_lo: r.x_lo,
                    x_hi: r.x_hi,
                    y_lo: r.y_lo,
                    y_hi: r.y_hi,
                },
            )
            .unwrap();
        log.assert_eq_json(
            "query-extremes",
            json!({"rect":[r.x_lo,r.x_hi,r.y_lo,r.y_hi]}),
            json!({"sum":want_sum,"empty":want_empty,"oracle_sum":osum,"oracle_nonempty":ononempty}),
            json!({"sum":got.sum,"empty":got.empty,"oracle_sum":osum,"oracle_nonempty":ononempty}),
        );
    }

    // 倒矩形：INVERTED_RECT（400），与空矩形区分
    let e = store
        .query(t, Some(1), &rect(10, 1, i64::MIN, i64::MAX))
        .unwrap_err();
    log.assert_eq_json(
        "inverted-rect",
        json!({"rect":[10,1,i64::MIN,i64::MAX]}),
        json!({"code":"INVERTED_RECT","http":400}),
        json!({"code":e.code(),"http":e.http_status()}),
    );
}

/// 场景 C：未注册坐标拒绝（不就近插入），且原状态不被污染。
#[test]
fn case_c_unregistered_coordinate_rejected() {
    let tmp = TempDir::new("hand-c");
    let store = Store::open(tmp.path()).unwrap();
    let mut log = TestLog::new("case_c");
    let reg = store.register_table(vec![1, 2], vec![10, 20]).unwrap();
    let t = reg.table_id;

    let v1 = store
        .commit_batch(t, None, vec![p(1, 10, 4)])
        .unwrap()
        .version;
    assert_eq!(v1, 1);

    // x 未注册
    let e = store.commit_batch(t, None, vec![p(3, 10, 1)]).unwrap_err();
    log.assert_eq_json(
        "unregistered-x",
        json!({"updates":[[3,10,1]]}),
        json!({"code":"COORDINATE_NOT_REGISTERED","http":422}),
        json!({"code":e.code(),"http":e.http_status()}),
    );
    // y 未注册（即使 x 已注册）
    let e = store.commit_batch(t, None, vec![p(1, 30, 1)]).unwrap_err();
    assert_eq!(e.code(), "COORDINATE_NOT_REGISTERED");
    // “看起来最近”的坐标 (2,20) 也不能被误插入：值必须仍为 0
    let near = store.query(t, None, &rect(2, 2, 20, 20)).unwrap();
    log.assert_eq_json(
        "no-misinsertion",
        json!({"point":[2,20]}),
        json!({"sum":0}),
        json!({"sum":near.sum}),
    );
    // 拒绝没有产生新版本：最新仍是 v1
    let ok = store.query(t, None, &rect(1, 1, 10, 10)).unwrap();
    assert_eq!(ok.version, 1);
    assert_eq!(ok.sum, 4);
}
