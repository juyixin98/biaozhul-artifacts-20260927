//! The JSONL request log must carry a run id, key intermediate state and the
//! failure category, so a reported problem can be replayed offline.

mod common;

use common::*;

#[tokio::test]
async fn request_log_records_success_and_failure_category() {
    let env = TestEnv::new(16, 16);
    env.create("logged", b"banana", Some(2)).await;

    // one successful multi-pattern search
    let (s, res, run_success) = env
        .request(
            "POST",
            "/indexes/logged/search",
            Some(serde_json::json!({ "patterns": [b64("ana"), b64("zzz")] })),
        )
        .await;
    assert_eq!(s, 200);
    assert_eq!(positions_of(&res, 0), vec![1, 3]);

    // one resource failure (oversized create) and one state conflict
    let (s, _, run_toobig) = env
        .request(
            "POST",
            "/indexes",
            Some(serde_json::json!({ "name": "big", "text": b64(vec![0u8; 50]) })),
        )
        .await;
    assert_eq!(s, 413);
    let (s, _, run_missing) = env
        .request(
            "POST",
            "/indexes/nope/search",
            Some(serde_json::json!({ "pattern": b64("a") })),
        )
        .await;
    assert_eq!(s, 404);

    let log = env.request_log();
    let lines: Vec<serde_json::Value> = log
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert!(lines.len() >= 4, "log lines: {}", lines.len());

    let find = |id: &str| {
        lines
            .iter()
            .find(|v| v["run_id"] == id)
            .unwrap_or_else(|| panic!("run {id} not in log"))
    };

    let ok = find(&run_success);
    assert_eq!(ok["path"], "/indexes/logged/search");
    assert_eq!(ok["status"], 200);
    // key intermediate state: final searched interval and total hits
    assert_eq!(ok["patterns"], 2);
    assert_eq!(ok["hit_count"], 2); // "ana" twice, "zzz" zero -> total 2
    assert!(ok["interval"].is_array());
    assert!(ok["error_category"].is_null());

    let big = find(&run_toobig);
    assert_eq!(big["status"], 413);
    assert_eq!(big["error_category"], "resource_exhausted");
    assert_eq!(big["error_code"], "text_too_large");

    let missing = find(&run_missing);
    assert_eq!(missing["error_category"], "state_conflict");
    assert_eq!(missing["error_code"], "index_not_found");

    // Every line carries a run id and a timestamp for replay correlation.
    for l in &lines {
        assert!(l["run_id"].as_str().unwrap().len() >= 8);
        assert!(l["ts_unix_ms"].as_u64().is_some());
        assert!(l["duration_ms"].as_u64().is_some());
    }
}
