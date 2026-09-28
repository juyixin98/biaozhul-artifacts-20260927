//! End-to-end HTTP contract: exact match positions, edge-case semantics, and
//! the four-way error taxonomy (invalid input / state conflict / resource
//! exhausted / compute failure).

mod common;

use common::*;

#[tokio::test]
async fn health_and_run_id_round_trip() {
    let env = TestEnv::new(1 << 20, 16);
    let (status, body, run_id) = env.request("GET", "/health", None).await;
    assert_eq!(status, 200);
    assert_eq!(body["status"], "ok");
    assert!(!run_id.is_empty(), "every response carries x-run-id");
}

#[tokio::test]
async fn create_search_and_delete_lifecycle() {
    let env = TestEnv::new(1 << 20, 16);
    let text = b"banana";
    let (s, meta) = env.create("fruity", text, Some(3)).await;
    assert_eq!(s, 201, "{meta}");
    assert_eq!(meta["text_len"], 6);
    assert_eq!(meta["sample_interval"], 3);
    assert_eq!(meta["sha256"].as_str().unwrap().len(), 64);

    let (s, listed, _) = env.request("GET", "/indexes", None).await;
    assert_eq!(s, 200);
    assert_eq!(listed["indexes"][0]["name"], "fruity");

    // exact, literal expected positions (not produced by the index core)
    let (s, res) = env
        .search_b64("fruity", &[b64("ana"), b64("na"), b64("xyz")])
        .await;
    assert_eq!(s, 200);
    assert_eq!(positions_of(&res, 0), vec![1, 3]); // "ana" overlaps
    assert_eq!(positions_of(&res, 1), vec![2, 4]); // "na"
    assert_eq!(positions_of(&res, 2), Vec::<u64>::new());
    // half-open interval; count == hi - lo == 2 for "ana". Rows 2 and 3 of
    // the SA are the suffixes "ana" (offset 3) and "anana" (offset 1).
    assert_eq!(res["results"][0]["lo"], 2);
    assert_eq!(res["results"][0]["hi"], 4);
    assert_eq!(res["results"][0]["count"], 2);

    let (s, _, _) = env.request("DELETE", "/indexes/fruity", None).await;
    assert_eq!(s, 200);
    let (s, err, _) = env
        .request(
            "POST",
            "/indexes/fruity/search",
            Some(serde_json::json!({ "pattern": b64("a") })),
        )
        .await;
    assert_eq!(s, 404);
    assert_eq!(err["category"], "state_conflict");
    assert_eq!(err["code"], "index_not_found");
}

#[tokio::test]
async fn empty_and_overlong_patterns_have_defined_semantics() {
    let env = TestEnv::new(1 << 20, 16);
    let text = b"abc";
    env.create("t", text, Some(2)).await;

    // empty pattern -> every suffix boundary 0..=3, interval [0,n)
    let (s, res) = env.search_b64("t", &[b64("")]).await;
    assert_eq!(s, 200);
    assert_eq!(res["results"][0]["lo"], 0);
    assert_eq!(res["results"][0]["hi"], 4);
    assert_eq!(res["results"][0]["count"], 4);
    assert_eq!(positions_of(&res, 0), vec![0, 1, 2, 3]);
    assert_eq!(res["results"][0]["empty_pattern"], true);

    // pattern longer than text -> empty interval, still 200
    let (s, res) = env.search_b64("t", &[b64("abcd")]).await;
    assert_eq!(s, 200);
    assert_eq!(res["results"][0]["lo"], 0);
    assert_eq!(res["results"][0]["hi"], 0);
    assert_eq!(res["results"][0]["count"], 0);
}

#[tokio::test]
async fn overlapping_matches_on_highly_repetitive_text() {
    let env = TestEnv::new(1 << 20, 16);
    let text: Vec<u8> = b"aaaaa".to_vec();
    env.create("rep", &text, Some(1)).await;
    // Expected via independent scan: "aa" at 0,1,2,3 ; "aaa" at 0,1,2.
    let (s, res) = env
        .search_b64("rep", &[b64("aa"), b64("aaa"), b64("aaaaa")])
        .await;
    assert_eq!(s, 200);
    assert_eq!(positions_of(&res, 0), expected_positions(&text, b"aa"));
    assert_eq!(positions_of(&res, 0), vec![0, 1, 2, 3]);
    assert_eq!(positions_of(&res, 1), vec![0, 1, 2]);
    assert_eq!(positions_of(&res, 2), vec![0]);
}

#[tokio::test]
async fn binary_zero_byte_patterns() {
    let env = TestEnv::new(1 << 20, 16);
    let text: Vec<u8> = vec![0x00, 0x00, 0xFF, 0x00, 0x00, 0x00];
    env.create("bin", &text, Some(4)).await;

    let (s, res) = env
        .search_b64(
            "bin",
            &[
                b64([0x00]),
                b64([0x00, 0x00]),
                b64([0xFF]),
                b64([0xFF, 0x00]),
                b64([0x01]),
            ],
        )
        .await;
    assert_eq!(s, 200);
    assert_eq!(positions_of(&res, 0), vec![0, 1, 3, 4, 5]);
    assert_eq!(positions_of(&res, 1), vec![0, 3, 4]); // overlapping pairs
    assert_eq!(positions_of(&res, 2), vec![2]);
    assert_eq!(positions_of(&res, 3), vec![2]);
    assert_eq!(positions_of(&res, 4), Vec::<u64>::new());

    // every result equals what a literal scan says
    for (i, pat) in [&[0x00][..], &[0x00, 0x00], &[0xFF], &[0xFF, 0x00], &[0x01]]
        .iter()
        .enumerate()
    {
        assert_eq!(positions_of(&res, i), expected_positions(&text, pat));
    }
}

#[tokio::test]
async fn invalid_inputs_are_distinct_from_conflicts_and_limits() {
    let env = TestEnv::new(16, 16); // 16-byte cap
    env.create("ok", b"0123456789abcdef", None).await;

    // invalid input: bad base64
    let (s, err, _) = env
        .request(
            "POST",
            "/indexes",
            Some(serde_json::json!({ "name": "bad", "text": "%%%not-base64" })),
        )
        .await;
    assert_eq!(s, 400);
    assert_eq!(err["category"], "invalid_input");
    assert_eq!(err["code"], "bad_base64");

    // invalid input: empty text
    let (s, err, _) = env
        .request(
            "POST",
            "/indexes",
            Some(serde_json::json!({ "name": "bad", "text": b64("") })),
        )
        .await;
    assert_eq!(s, 400);
    assert_eq!(err["code"], "empty_text");

    // invalid input: bad name (path traversal)
    let (s, err, _) = env
        .request(
            "POST",
            "/indexes",
            Some(serde_json::json!({ "name": "../evil", "text": b64("abc") })),
        )
        .await;
    assert_eq!(s, 400);
    assert_eq!(err["code"], "bad_name");

    // invalid input: sample interval out of range
    let (s, err, _) = env
        .request(
            "POST",
            "/indexes",
            Some(serde_json::json!({ "name": "badk", "text": b64("abc"), "sample_interval": 0 })),
        )
        .await;
    assert_eq!(s, 400);
    assert_eq!(err["code"], "bad_sample_interval");

    // resource exhaustion: over the size cap -> 413, distinct category
    let (s, err, _) = env
        .request(
            "POST",
            "/indexes",
            Some(serde_json::json!({ "name": "big", "text": b64(vec![0u8; 17]) })),
        )
        .await;
    assert_eq!(s, 413);
    assert_eq!(err["category"], "resource_exhausted");
    assert_eq!(err["code"], "text_too_large");

    // state conflict: duplicate create
    let (s, err, _) = env
        .request(
            "POST",
            "/indexes",
            Some(serde_json::json!({ "name": "ok", "text": b64("zz") })),
        )
        .await;
    assert_eq!(s, 409);
    assert_eq!(err["category"], "state_conflict");
    assert_eq!(err["code"], "index_already_exists");

    // malformed JSON body -> framework 400 (not a 500)
    let (status, _v, _) = env.request_raw("POST", "/indexes", "{ not json").await;
    assert_eq!(status, 400);
}
