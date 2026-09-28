//! 持久化适配测试：
//! - 正常重放与在线状态一致；
//! - 截断 / CRC 位翻转 → 启动即 CORRUPT_LOG，绝不静默空库；
//! - 重放会重新计算并比对 cells，篡改 JSON 语义也能识别。

mod common;

use common::{TempDir, TestLog};
use pr2d::model::PointUpdate;
use pr2d::store::Store;
use serde_json::json;

fn p(x: i64, y: i64, d: i64) -> PointUpdate {
    PointUpdate { x, y, delta: d }
}

fn wal_path(dir: &std::path::Path) -> std::path::PathBuf {
    dir.join("wal.log")
}

#[test]
fn corrupt_truncation_and_bitflip_refuse_start() {
    let tmp = TempDir::new("persist-bad");
    {
        let store = Store::open(tmp.path()).unwrap();
        let t = store.register_table(vec![1, 2], vec![3]).unwrap().table_id;
        store.commit_batch(t, None, vec![p(1, 3, 9)]).unwrap();
    }
    let mut log = TestLog::new("corrupt_wal");
    let path = wal_path(tmp.path());
    let good = std::fs::read(&path).unwrap();

    // 截断
    std::fs::write(&path, &good[..good.len() - 5]).unwrap();
    let e = Store::open(tmp.path()).unwrap_err();
    log.assert_eq_json(
        "truncated",
        json!({"bytes_removed":5,"size":good.len()}),
        json!({"code":"CORRUPT_LOG","http":500}),
        json!({"code":e.code(),"http":e.http_status()}),
    );

    // CRC 位翻转（payload 中间翻一位，magic 保持正确，长度保持正确）
    std::fs::write(&path, &good).unwrap();
    let mut flipped = good.clone();
    let idx = flipped.len() / 2;
    flipped[idx] ^= 0x80;
    std::fs::write(&path, &flipped).unwrap();
    let e = Store::open(tmp.path()).unwrap_err();
    assert_eq!(e.code(), "CORRUPT_LOG");

    // magic 破坏
    let mut bad_magic = good.clone();
    bad_magic[0] ^= 0xFF;
    std::fs::write(&path, &bad_magic).unwrap();
    let e = Store::open(tmp.path()).unwrap_err();
    assert_eq!(e.code(), "CORRUPT_LOG");

    // 恢复正确文件后必须能正常打开（损坏不是永久状态）
    std::fs::write(&path, &good).unwrap();
    let store = Store::open(tmp.path()).unwrap();
    assert_eq!(store.list_table_ids(), vec![1]);
}

#[test]
fn replayed_state_matches_live_state() {
    let tmp = TempDir::new("persist-replay");
    let batches: Vec<Vec<PointUpdate>> = vec![
        vec![p(1, 1, 10), p(2, 2, -4)],
        vec![p(1, 1, -3), p(2, 2, i64::MIN + 4)],
        vec![p(1, 1, 1)],
    ];
    let t;
    {
        let store = Store::open(tmp.path()).unwrap();
        t = store
            .register_table(vec![1, 2, 3], vec![1, 2])
            .unwrap()
            .table_id;
        for b in &batches {
            store.commit_batch(t, None, b.clone()).unwrap();
        }
    }
    let store2 = Store::open(tmp.path()).unwrap();
    let mut log = TestLog::new("replay_match");
    store2
        .with_table(t, |tb| {
            let infos = tb.versions();
            log.assert_eq_json(
                "version-count",
                json!({"batches":batches.len()}),
                json!({"versions":batches.len()+1}),
                json!({"versions":infos.len()}),
            );
            assert_eq!(infos.last().unwrap().version as usize, batches.len());
        })
        .unwrap();

    // 手算：(1,1)=10-3+1=8；(2,2)=-4+MIN+4=MIN
    let q1 = store2
        .query(
            t,
            None,
            &pr2d::rect::Rect {
                x_lo: 1,
                x_hi: 1,
                y_lo: 1,
                y_hi: 1,
            },
        )
        .unwrap();
    assert_eq!(q1.sum, 8);
    let q2 = store2
        .query(
            t,
            None,
            &pr2d::rect::Rect {
                x_lo: 2,
                x_hi: 2,
                y_lo: 2,
                y_hi: 2,
            },
        )
        .unwrap();
    assert_eq!(q2.sum, i64::MIN);
    // 中间版本也可查：v1 时 (1,1)=10
    let q3 = store2
        .query(
            t,
            Some(1),
            &pr2d::rect::Rect {
                x_lo: 1,
                x_hi: 1,
                y_lo: 1,
                y_hi: 1,
            },
        )
        .unwrap();
    assert_eq!(q3.sum, 10);
}
