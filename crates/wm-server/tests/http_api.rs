//! Black-box HTTP tests against a real Axum server bound to an ephemeral
//! port. Expected answers come from an independent in-test sort/linear
//! oracle over the posted values; the kernel is never the oracle.

use serde_json::{json, Value};
use tempfile::tempdir;
use wm_server::app::{build_app, serve_local};

async fn post(client: &reqwest_lite::Client, url: &str, body: Value) -> (u16, Value) {
    client.post_json(url, body).await
}

async fn get(client: &reqwest_lite::Client, url: &str) -> (u16, Value) {
    client.get_json(url).await
}

async fn delete(client: &reqwest_lite::Client, url: &str) -> (u16, Value) {
    client.delete_json(url).await
}

fn sorted(v: &[i64]) -> Vec<i64> {
    let mut s = v.to_vec();
    s.sort_unstable();
    s
}

#[tokio::test]
async fn full_lifecycle_and_oracle_verified_queries() {
    let dir = tempdir().unwrap();
    let app = build_app(dir.path()).unwrap();
    let addr = serve_local(app).await.unwrap();
    let base = format!("http://{addr}");
    let client = reqwest_lite::Client::new();

    // Service info and health.
    let (status, info) = get(&client, &format!("{base}/health")).await;
    assert_eq!(status, 200);
    assert_eq!(info["data"]["status"], "ok");
    assert!(info["request_id"].is_string());

    let values = json!([5, 2, 5, 0, -3, 2, i64::MAX, i64::MIN, -1, -1]);
    let values_arr: Vec<i64> = serde_json::from_value(values.clone()).unwrap();

    // Create with a client-chosen request id that must be echoed.
    let (status, created) = client
        .post_json_with_id(
            &format!("{base}/indexes/demo"),
            json!({"values": values, "overwrite": false}),
            "test-req-001",
        )
        .await;
    assert_eq!(status, 201);
    assert_eq!(created["request_id"], "test-req-001");
    assert_eq!(created["data"]["name"], "demo");
    assert_eq!(created["data"]["len"], 10);
    assert_eq!(created["diagnostics"]["index"]["format_version"], 1);
    assert!(created["diagnostics"]["steps"].is_array());

    // Recreate without overwrite -> specific conflict category.
    let (status, err) = post(
        &client,
        &format!("{base}/indexes/demo"),
        json!({"values": [1]}),
    )
    .await;
    assert_eq!(status, 409);
    assert_eq!(err["error"]["kind"], "INDEX_ALREADY_EXISTS");
    assert_eq!(err["ok"], false);
    assert!(err["request_id"].is_string());

    // Listing reports the index.
    let (_, listing) = get(&client, &format!("{base}/indexes")).await;
    assert_eq!(listing["data"]["indexes"][0]["name"], "demo");
    assert_eq!(listing["data"]["corrupt_entries"], json!([]));

    let s = sorted(&values_arr);

    // Quantile across the whole range vs independent oracle.
    for (k, expected) in s.iter().enumerate() {
        let (status, body) = post(
            &client,
            &format!("{base}/query"),
            json!({"op": "quantile", "index": "demo", "l": 0, "r": 10, "k": k}),
        )
        .await;
        assert_eq!(status, 200, "body: {body}");
        assert_eq!(
            body["data"]["value"],
            json!(expected),
            "k={k} oracle={expected} body={body}"
        );
        // The trace must show one branch per level with concrete intervals.
        let steps = &body["diagnostics"]["trace"]["steps"];
        assert!(steps.is_array());
        assert!(steps[0]["range_before"].is_array());
        assert_eq!(
            body["data"]["value"],
            body["diagnostics"]["trace"]["result_value"]
        );
    }

    // Count query checked against a linear scan oracle.
    let lo: i64 = -1;
    let hi: i64 = 5;
    let expected_count = values_arr.iter().filter(|&&x| lo <= x && x < hi).count();
    let (_, body) = post(
        &client,
        &format!("{base}/query"),
        json!({"op": "count", "index": "demo", "l": 0, "r": 10, "lo": lo, "hi": hi}),
    )
    .await;
    assert_eq!(body["data"]["count"], json!(expected_count as u64));
    assert_eq!(
        body["diagnostics"]["trace"]["subtraction"]["formula"],
        "count([lo,hi)) = count(< hi) - count(< lo)"
    );

    // Predecessor / successor concrete results.
    let (_, body) = post(
        &client,
        &format!("{base}/query"),
        json!({"op": "successor", "index": "demo", "l": 0, "r": 10, "x": 2}),
    )
    .await;
    assert_eq!(body["data"]["value"], json!(5));
    assert_eq!(body["data"]["present"], true);

    let (_, body) = post(
        &client,
        &format!("{base}/query"),
        json!({"op": "predecessor", "index": "demo", "l": 0, "r": 10, "x": i64::MIN}),
    )
    .await;
    assert_eq!(body["data"]["value"], Value::Null);
    assert_eq!(body["data"]["present"], true);

    let (_, body) = post(
        &client,
        &format!("{base}/query"),
        json!({"op": "successor", "index": "demo", "l": 0, "r": 10, "x": i64::MAX}),
    )
    .await;
    assert_eq!(body["data"]["value"], Value::Null);
    assert_eq!(body["data"]["exists"], false);

    // Delete then query -> 404 with a specific kind.
    let (status, _) = delete(&client, &format!("{base}/indexes/demo")).await;
    assert_eq!(status, 200);
    let (status, err) = post(
        &client,
        &format!("{base}/query"),
        json!({"op": "quantile", "index": "demo", "l": 0, "r": 1, "k": 0}),
    )
    .await;
    assert_eq!(status, 404);
    assert_eq!(err["error"]["kind"], "INDEX_NOT_FOUND");
}

#[tokio::test]
async fn rejects_invalid_requests_with_specific_categories() {
    let dir = tempdir().unwrap();
    let app = build_app(dir.path()).unwrap();
    let addr = serve_local(app).await.unwrap();
    let base = format!("http://{addr}");
    let client = reqwest_lite::Client::new();

    // Bootstrap one index.
    let (status, _) = post(
        &client,
        &format!("{base}/indexes/v"),
        json!({"values": [1, 2, 3, 4]}),
    )
    .await;
    assert_eq!(status, 201);

    let cases = [
        (
            json!({"op": "quantile", "index": "v", "l": 1, "r": 1, "k": 0}),
            422,
            "EMPTY_RANGE",
        ),
        (
            json!({"op": "quantile", "index": "v", "l": 0, "r": 4, "k": 4}),
            422,
            "K_OUT_OF_BOUNDS",
        ),
        (
            json!({"op": "quantile", "index": "v", "l": 2, "r": 9, "k": 0}),
            422,
            "RANGE_OUT_OF_BOUNDS",
        ),
        (
            json!({"op": "quantile", "index": "v", "l": 3, "r": 2, "k": 0}),
            422,
            "RANGE_OUT_OF_BOUNDS",
        ),
    ];
    for (body, expected_status, expected_kind) in cases {
        let (status, resp) = post(&client, &format!("{base}/query"), body).await;
        assert_eq!(status, expected_status, "resp={resp}");
        assert_eq!(resp["error"]["kind"], json!(expected_kind), "resp={resp}");
        assert_eq!(resp["ok"], false);
        assert!(resp["error"]["message"].is_string());
    }

    // Unknown op and malformed JSON are rejected as bad requests.
    let (status, resp) = post(
        &client,
        &format!("{base}/query"),
        json!({"op": "nope", "index": "v", "l": 0, "r": 1}),
    )
    .await;
    assert_eq!(status, 400);
    assert_eq!(resp["error"]["kind"], "INVALID_JSON");

    let (status, resp) = client
        .post_raw(&format!("{base}/query"), "{not json".to_string())
        .await;
    assert_eq!(status, 400);
    assert_eq!(resp["error"]["kind"], "INVALID_JSON");

    // Empty build input is a kernel rejection, not a 500.
    let (status, resp) = post(
        &client,
        &format!("{base}/indexes/empty"),
        json!({"values": []}),
    )
    .await;
    assert_eq!(status, 400);
    assert_eq!(resp["error"]["kind"], "EMPTY_VALUES");

    // Unknown route.
    let (status, resp) = get(&client, &format!("{base}/nope")).await;
    assert_eq!(status, 404);
    assert_eq!(resp["error"]["kind"], "ROUTE_NOT_FOUND");

    // Unsafe index name -> specific category.
    let (status, _resp) = post(
        &client,
        &format!("{base}/indexes/..%2fescape"),
        json!({"values": [1]}),
    )
    .await;
    assert!(status == 400 || status == 404, "status={status}");
}

#[tokio::test]
async fn request_ids_are_correlated_across_header_body_logs() {
    let dir = tempdir().unwrap();
    let app = build_app(dir.path()).unwrap();
    let addr = serve_local(app).await.unwrap();
    let client = reqwest_lite::Client::new();

    let (status, headers, body) = client
        .post_full(
            &format!("http://{addr}/indexes/corr"),
            json!({"values": [9, 8, 7]}),
            Some("corr-id-xyz"),
        )
        .await;
    assert_eq!(status, 201);
    assert_eq!(
        headers.get("x-request-id").map(|s| s.as_str()),
        Some("corr-id-xyz")
    );
    assert_eq!(body["request_id"], "corr-id-xyz");

    // Without a supplied id, the server generates one and still correlates.
    let (status, headers, body) = client
        .post_full(
            &format!("http://{addr}/query"),
            json!({"op": "quantile", "index": "corr", "l": 0, "r": 3, "k": 1}),
            None,
        )
        .await;
    assert_eq!(status, 200);
    let generated = headers
        .get("x-request-id")
        .cloned()
        .expect("generated id header");
    assert!(generated.starts_with("wm-"));
    assert_eq!(body["request_id"], json!(generated));
    assert_eq!(body["data"]["value"], json!(8));
}

/// Minimal HTTP/1.1 client over a plain TCP stream so the test suite needs
/// no external HTTP crate (the project deliberately pins a small dep set).
mod reqwest_lite {
    use serde_json::Value;
    use std::collections::HashMap;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpStream;

    pub struct Client;

    impl Client {
        pub fn new() -> Self {
            Client
        }

        async fn request(
            &self,
            method: &str,
            url: &str,
            body: Option<String>,
            request_id: Option<&str>,
        ) -> (u16, HashMap<String, String>, Value) {
            let (host, port, path) = split_url(url);
            let mut stream = TcpStream::connect(format!("{host}:{port}"))
                .await
                .expect("connect");
            let payload = body.unwrap_or_default();
            let mut req = format!(
                "{method} {path} HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n",
                payload.len()
            );
            if let Some(id) = request_id {
                req.push_str(&format!("X-Request-Id: {id}\r\n"));
            }
            req.push_str("\r\n");
            req.push_str(&payload);
            stream.write_all(req.as_bytes()).await.unwrap();

            let mut raw = Vec::new();
            stream.read_to_end(&mut raw).await.unwrap();
            parse_response(&raw)
        }

        pub async fn get_json(&self, url: &str) -> (u16, Value) {
            let (s, _, v) = self.request("GET", url, None, None).await;
            (s, v)
        }

        pub async fn post_json(&self, url: &str, body: Value) -> (u16, Value) {
            let (s, _, v) = self
                .request("POST", url, Some(body.to_string()), None)
                .await;
            (s, v)
        }

        pub async fn post_json_with_id(&self, url: &str, body: Value, id: &str) -> (u16, Value) {
            let (s, _, v) = self
                .request("POST", url, Some(body.to_string()), Some(id))
                .await;
            (s, v)
        }

        pub async fn post_raw(&self, url: &str, raw: String) -> (u16, Value) {
            let (s, _, v) = self.request("POST", url, Some(raw), None).await;
            (s, v)
        }

        pub async fn delete_json(&self, url: &str) -> (u16, Value) {
            let (s, _, v) = self.request("DELETE", url, None, None).await;
            (s, v)
        }

        pub async fn post_full(
            &self,
            url: &str,
            body: Value,
            id: Option<&str>,
        ) -> (u16, HashMap<String, String>, Value) {
            self.request("POST", url, Some(body.to_string()), id).await
        }
    }

    fn split_url(url: &str) -> (String, u16, String) {
        let rest = url.strip_prefix("http://").unwrap_or(url);
        let (hostport, path) = match rest.find('/') {
            Some(i) => (&rest[..i], rest[i..].to_string()),
            None => (rest, "/".to_string()),
        };
        let (host, port) = match hostport.split_once(':') {
            Some((h, p)) => (h.to_string(), p.parse().unwrap()),
            None => (hostport.to_string(), 80),
        };
        (host, port, path)
    }

    fn parse_response(raw: &[u8]) -> (u16, HashMap<String, String>, Value) {
        let text = String::from_utf8_lossy(raw);
        let split_at = text.find("\r\n\r\n").expect("headers end");
        let head = &text[..split_at];
        let body = &text[split_at + 4..];
        let mut lines = head.lines();
        let status: u16 = lines
            .next()
            .unwrap()
            .split_whitespace()
            .nth(1)
            .unwrap()
            .parse()
            .unwrap();
        let mut headers = HashMap::new();
        for line in lines {
            if let Some((k, v)) = line.split_once(':') {
                headers.insert(k.trim().to_lowercase(), v.trim().to_string());
            }
        }
        // Handle chunked transfer (axum may use it with Connection: close?
        // usually not; but strip a single content-length body robustly).
        let json_body = if let Some(enc) = headers.get("transfer-encoding") {
            if enc.contains("chunked") {
                dechunk(body)
            } else {
                body.to_string()
            }
        } else if let Some(len) = headers.get("content-length") {
            let n: usize = len.parse().unwrap_or(body.len());
            body[..body.len().min(n)].to_string()
        } else {
            body.to_string()
        };
        let value = serde_json::from_str(&json_body)
            .unwrap_or_else(|e| panic!("response body was not JSON: {e:?}\nbody={json_body:?}"));
        (status, headers, value)
    }

    fn dechunk(body: &str) -> String {
        let mut out = String::new();
        let mut rest = body;
        while let Some(line_end) = rest.find("\r\n") {
            let size_line = &rest[..line_end];
            let size = usize::from_str_radix(size_line.trim(), 16).unwrap_or(0);
            rest = &rest[line_end + 2..];
            if size == 0 {
                break;
            }
            out.push_str(&rest[..size.min(rest.len())]);
            rest = &rest[(size + 2).min(rest.len())..];
        }
        out
    }
}
