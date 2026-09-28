//! 夹具驱动测试：读取 `fixtures/scenarios.json`（人手计算 + Python 独立复核），
//! 对 pr2d 内核逐条执行并断言具体结果/错误类别。
//!
//! 这个测试保证：内核给出的答案必须等于夹具中手写的期望值；
//! `scripts/check_fixtures.py` 则保证：夹具期望值本身等于独立稀疏映射全扫描。
//! 两条链都不使用被测内核生成“参考答案”。

mod common;

use std::path::PathBuf;

use common::{TempDir, TestLog};
use pr2d::model::PointUpdate;
use pr2d::rect::Rect;
use pr2d::store::Store;
use serde_json::{json, Value};

fn fixture_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("fixtures/scenarios.json")
}

fn as_point(v: &Value) -> PointUpdate {
    PointUpdate {
        x: v["x"].as_i64().unwrap(),
        y: v["y"].as_i64().unwrap(),
        delta: v["delta"].as_i64().unwrap(),
    }
}

#[test]
fn fixture_scenarios_match_handwritten_expectations() {
    let raw = std::fs::read_to_string(fixture_path()).expect("read fixtures/scenarios.json");
    let doc: Value = serde_json::from_str(&raw).expect("fixtures must be valid JSON");
    assert_eq!(doc["schema"], "pr2d-fixtures/v1");

    for case in doc["cases"].as_array().unwrap() {
        run_case(case);
    }
}

fn run_case(case: &Value) {
    let name = case["name"].as_str().unwrap();
    let tmp = TempDir::new(&format!("fixt-{}", name.replace('_', "-")));
    let store = Store::open(tmp.path()).unwrap();
    let mut log = TestLog::new(&format!("fixture_{name}"));

    let xs: Vec<i64> = case["xs_register"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_i64().unwrap())
        .collect();
    let ys: Vec<i64> = case["ys_register"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_i64().unwrap())
        .collect();
    let reg = store.register_table(xs, ys).unwrap();
    let er = &case["expected_register"];
    log.assert_eq_json(
        "register",
        json!({"case":name}),
        json!({"nx":er["nx"],"ny":er["ny"],"dup_x":er["duplicate_x"],"dup_y":er["duplicate_y"]}),
        json!({"nx":reg.nx,"ny":reg.ny,"dup_x":reg.duplicate_x,"dup_y":reg.duplicate_y}),
    );
    let t = reg.table_id;

    // 正常批次（按夹具给出的 expected_version 断言）
    if let Some(batches) = case["batches"].as_array() {
        for b in batches {
            let updates: Vec<PointUpdate> = b["updates"]
                .as_array()
                .unwrap()
                .iter()
                .map(as_point)
                .collect();
            let base = b
                .get("base_version")
                .and_then(|v| v.as_i64())
                .map(|v| v as u64);
            let out = store.commit_batch(t, base, updates).unwrap();
            log.assert_eq_json(
                "batch",
                json!({"case":name,"updates":b["updates"]}),
                json!({"version":b["expected_version"]}),
                json!({"version":out.version}),
            );
        }
    }

    // 查询断言
    if let Some(queries) = case["queries"].as_array() {
        for q in queries {
            let r = Rect {
                x_lo: q["x_lo"].as_i64().unwrap(),
                x_hi: q["x_hi"].as_i64().unwrap(),
                y_lo: q["y_lo"].as_i64().unwrap(),
                y_hi: q["y_hi"].as_i64().unwrap(),
            };
            let version = q["version"].as_u64();
            let got = store.query(t, version, &r).unwrap();
            log.assert_eq_json(
                "query",
                json!({"case":name,"q":q}),
                json!({"sum":q["expected_sum"],"empty":q["expected_empty"]}),
                json!({"sum":got.sum,"empty":got.empty}),
            );
        }
    }

    // 拒绝/特殊序列（顺序执行，断言状态推进）
    if let Some(rejections) = case["rejections"].as_array() {
        for item in rejections {
            let kind = item["kind"].as_str().unwrap();
            let updates: Vec<PointUpdate> = item["updates"]
                .as_array()
                .unwrap_or(&Vec::new())
                .iter()
                .map(as_point)
                .collect();
            let base = item
                .get("base_version")
                .and_then(|v| v.as_i64())
                .map(|v| v as u64);
            match kind {
                "batch" => {
                    let e = store.commit_batch(t, base, updates).unwrap_err();
                    log.assert_eq_json(
                        "rejection",
                        json!({"case":name,"item":item}),
                        json!({"code":item["expected_code"]}),
                        json!({"code":e.code()}),
                    );
                }
                "success" => {
                    let out = store.commit_batch(t, base, updates).unwrap();
                    log.assert_eq_json(
                        "success-item",
                        json!({"case":name,"item":item}),
                        json!({"version":item["expected_version"]}),
                        json!({"version":out.version}),
                    );
                }
                "query" => {
                    let r = Rect {
                        x_lo: item["x_lo"].as_i64().unwrap(),
                        x_hi: item["x_hi"].as_i64().unwrap(),
                        y_lo: item["y_lo"].as_i64().unwrap(),
                        y_hi: item["y_hi"].as_i64().unwrap(),
                    };
                    let version = item
                        .get("version")
                        .and_then(|v| v.as_i64())
                        .map(|v| v as u64);
                    let e = store.query(t, version, &r).unwrap_err();
                    log.assert_eq_json(
                        "query-rejection",
                        json!({"case":name,"item":item}),
                        json!({"code":item["expected_code"]}),
                        json!({"code":e.code()}),
                    );
                }
                other => panic!("unknown rejection kind {other}"),
            }
        }
    }

    // 拒绝序列之后的状态断言（证明拒绝没有污染版本）
    if let Some(qs) = case["post_rejection_queries"].as_array() {
        for q in qs {
            let r = Rect {
                x_lo: q["x_lo"].as_i64().unwrap(),
                x_hi: q["x_hi"].as_i64().unwrap(),
                y_lo: q["y_lo"].as_i64().unwrap(),
                y_hi: q["y_hi"].as_i64().unwrap(),
            };
            let version = q["version"].as_u64();
            let got = store.query(t, version, &r).unwrap();
            log.assert_eq_json(
                "post-rejection-query",
                json!({"case":name,"q":q}),
                json!({"sum":q["expected_sum"],"empty":q["expected_empty"]}),
                json!({"sum":got.sum,"empty":got.empty}),
            );
        }
    }
}
