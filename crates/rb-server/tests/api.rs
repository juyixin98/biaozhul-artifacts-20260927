//! HTTP 接口集成测试：断言具体结果、稳定错误代码、请求身份关联与损坏拒绝。
//!
//! 不启动真实 TCP 端口，直接通过 tower `oneshot` 驱动 [`Router`]。

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use rb_persist::Store;
use rb_server::{app, state::AppState};
use serde_json::Value;
use tower::ServiceExt;

fn temp_dir(tag: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "rbsrv-test-{}-{}-{}",
        tag,
        std::process::id(),
        COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

struct App {
    state: AppState,
    dir: std::path::PathBuf,
}

impl App {
    fn new() -> Self {
        let dir = temp_dir("app");
        let store = Store::open(&dir).unwrap();
        App {
            state: AppState::new(store),
            dir,
        }
    }

    fn fresh_state(&self) -> AppState {
        AppState::new(Store::open(&self.dir).unwrap())
    }

    async fn call(&self, req: Request<Body>) -> (StatusCode, Value, String) {
        call_router(app(self.state.clone()), req).await
    }

    async fn call_fresh(&self, req: Request<Body>) -> (StatusCode, Value, String) {
        call_router(app(self.fresh_state()), req).await
    }
}

async fn call_router(app: axum::Router, req: Request<Body>) -> (StatusCode, Value, String) {
    let resp = app.oneshot(req).await.unwrap();
    let status = resp.status();
    let header_id = resp
        .headers()
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let value: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, value, header_id)
}

fn req(method: &str, uri: &str) -> Request<Body> {
    Request::builder()
        .method(method)
        .uri(uri)
        .body(Body::empty())
        .unwrap()
}

fn json_req(method: &str, uri: &str, body: Value) -> Request<Body> {
    Request::builder()
        .method(method)
        .uri(uri)
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(&body).unwrap()))
        .unwrap()
}

#[tokio::test]
async fn healthz_reports_version_and_threshold() {
    let app = App::new();
    let (status, body, _) = app.call(req("GET", "/healthz")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["result"]["status"], "ok");
    assert_eq!(body["result"]["threshold"], 4096);
    assert_eq!(body["result"]["container_bits"], 65536);
    assert_eq!(body["format_version"], "0x00010000");
}

#[tokio::test]
async fn request_id_is_echoed_and_correlated() {
    let app = App::new();
    // 客户端指定 id：响应头与响应体必须一致回显
    let mut r = req("GET", "/healthz");
    r.headers_mut()
        .insert("x-request-id", "trace-abc-123".parse().unwrap());
    let (status, body, header) = app.call(r).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(header, "trace-abc-123");
    assert_eq!(body["request_id"], "trace-abc-123");

    // 未指定：服务端生成非空 id
    let (_, body, header) = app.call(req("GET", "/healthz")).await;
    assert!(!header.is_empty());
    assert_eq!(body["request_id"], header);
}

#[tokio::test]
async fn create_threshold_containers_reports_exact_kinds() {
    let app = App::new();
    // 4096 → array 容器
    let body = serde_json::json!({ "values": (0..4096u32).collect::<Vec<_>>() });
    let (status, resp, _) = app.call(json_req("POST", "/v1/sets?name=arr", body)).await;
    assert_eq!(status, StatusCode::CREATED);
    let r = &resp["result"];
    assert_eq!(r["cardinality"], 4096);
    assert_eq!(r["containers"], 1);
    assert_eq!(r["container_kinds"]["array"], 1);
    assert_eq!(r["container_kinds"]["bitmap"], 0);
    assert_eq!(r["persisted"], true);
    assert!(resp["errors"].is_null() || resp["errors"].as_array().unwrap().is_empty());

    // 5000 → bitmap 容器
    let vals: Vec<u32> = (0..5000u32).map(|i| i * 11 % 65536).collect();
    let body = serde_json::json!({ "values": vals });
    let (status, resp, _) = app.call(json_req("POST", "/v1/sets?name=bit", body)).await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(resp["result"]["cardinality"], 5000);
    assert_eq!(resp["result"]["container_kinds"]["bitmap"], 1);
    assert_eq!(resp["result"]["container_kinds"]["array"], 0);
}

#[tokio::test]
async fn contains_rank_select_concrete_values() {
    let app = App::new();
    let body = serde_json::json!({ "values": [0u32, 10, 20, 100, 65536, 65537, u32::MAX] });
    let (status, _, _) = app.call(json_req("POST", "/v1/sets?name=s", body)).await;
    assert_eq!(status, StatusCode::CREATED);

    let (_, b, _) = app.call(req("GET", "/v1/sets/s/contains/100")).await;
    assert_eq!(b["result"]["contains"], true);
    let (_, b, _) = app.call(req("GET", "/v1/sets/s/contains/101")).await;
    assert_eq!(b["result"]["contains"], false);

    // rank 具体值
    let (_, b, _) = app.call(req("GET", "/v1/sets/s/rank/65536")).await;
    assert_eq!(b["result"]["rank"], 4, "elements < 65536: 0,10,20,100");
    let (_, b, _) = app.call(req("GET", "/v1/sets/s/rank/0")).await;
    assert_eq!(b["result"]["rank"], 0);

    // select 具体值与越界 note
    let (_, b, _) = app.call(req("GET", "/v1/sets/s/select/4")).await;
    assert_eq!(b["result"]["value"], 65536);
    let (_, b, _) = app.call(req("GET", "/v1/sets/s/select/99")).await;
    assert_eq!(b["result"]["value"], Value::Null);
    let notes = b["notes"].as_array().unwrap();
    assert!(notes
        .iter()
        .any(|n| n.as_str().unwrap().contains(">= cardinality")));

    // rank/select 互逆：对每个元素 select(i) = e ⇒ rank(e) = i
    for (i, e) in [0u32, 10, 20, 100, 65536, 65537, u32::MAX]
        .iter()
        .enumerate()
    {
        let (_, rb, _) = app.call(req("GET", &format!("/v1/sets/s/rank/{e}"))).await;
        assert_eq!(rb["result"]["rank"], i as u64, "rank({e})");
    }
}

#[tokio::test]
async fn set_operations_return_exact_results_and_steps() {
    let app = App::new();
    app.call(json_req(
        "POST",
        "/v1/sets?name=a",
        serde_json::json!({"values": [1u32,2,3,10,100]}),
    ))
    .await;
    app.call(json_req(
        "POST",
        "/v1/sets?name=b",
        serde_json::json!({"values": [2u32,3,4,200]}),
    ))
    .await;

    // 交集 = {2,3}
    let (status, body, _) = app
        .call(json_req(
            "POST",
            "/v1/sets/a/intersect",
            serde_json::json!({"with": "b"}),
        ))
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["result"]["cardinality"], 2);
    assert_eq!(body["result"]["persisted"], false);
    let stages: Vec<&str> = body["steps"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["stage"].as_str().unwrap())
        .collect();
    assert_eq!(
        stages,
        vec!["load:left", "load:right", "op:intersect", "representation"]
    );
    // 非退化说明必须出现在 notes
    assert!(body["notes"].as_array().unwrap().iter().any(|n| n
        .as_str()
        .unwrap()
        .contains("no full integer-set expansion")));

    // 并集 = {1,2,3,4,10,100,200}
    let (_, body, _) = app
        .call(json_req(
            "POST",
            "/v1/sets/a/union",
            serde_json::json!({"with": "b"}),
        ))
        .await;
    assert_eq!(body["result"]["cardinality"], 7);

    // 差集 a\b = {1,10,100}
    let (_, body, _) = app
        .call(json_req(
            "POST",
            "/v1/sets/a/difference",
            serde_json::json!({"with": "b"}),
        ))
        .await;
    assert_eq!(body["result"]["cardinality"], 3);

    // 运算结果不落盘：集合列表仍只有 a,b
    let (_, body, _) = app.call(req("GET", "/v1/sets")).await;
    assert_eq!(body["result"]["sets"], serde_json::json!(["a", "b"]));
}

#[tokio::test]
async fn errors_have_stable_codes_and_request_id() {
    let app = App::new();
    // 404
    let (status, body, header) = app.call(req("GET", "/v1/sets/nope")).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["errors"][0]["code"], "set_not_found");
    assert_eq!(body["request_id"], header, "error body carries request id");

    // 指定的 request id 也必须进入错误体
    let mut r = req("GET", "/v1/sets/nope");
    r.headers_mut()
        .insert("x-request-id", "err-trace-1".parse().unwrap());
    let (_, body, header) = app.call(r).await;
    assert_eq!(header, "err-trace-1");
    assert_eq!(body["request_id"], "err-trace-1");

    // 非法名（路径逃逸尝试）
    let (status, body, _) = app
        .call(json_req(
            "PUT",
            "/v1/sets/..%2fetc",
            serde_json::json!({"values": []}),
        ))
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["errors"][0]["code"], "invalid_name");

    // 冲突
    app.call(json_req(
        "POST",
        "/v1/sets?name=dup",
        serde_json::json!({"values": [1], "expect_new": true}),
    ))
    .await;
    let (status, _, _) = app
        .call(json_req(
            "POST",
            "/v1/sets?name=dup",
            serde_json::json!({"values": [2], "expect_new": true}),
        ))
        .await;
    assert_eq!(status, StatusCode::CONFLICT);
}

#[tokio::test]
async fn corrupted_file_on_disk_is_rejected_with_category() {
    let app = App::new();
    let (status, _, _) = app
        .call(json_req(
            "POST",
            "/v1/sets?name=victim",
            serde_json::json!({"values": (0..5000u32).collect::<Vec<_>>()}),
        ))
        .await;
    assert_eq!(status, StatusCode::CREATED);

    let path = app.dir.join("victim.rbs");
    let mut bytes = std::fs::read(&path).unwrap();

    // 1) 翻转体区负载字节 → 必须 422 + corrupt_body_checksum（用无缓存的全新 state）
    let n = u32::from_le_bytes(bytes[8..12].try_into().unwrap()) as usize;
    let payload_start = 16 + n * 16;
    bytes[payload_start] ^= 0xFF;
    std::fs::write(&path, &bytes).unwrap();
    let (status, body, _) = app.call_fresh(req("GET", "/v1/sets/victim")).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["errors"][0]["code"], "corrupt_body_checksum");
    assert!(body["result"].is_null());

    // 2) 截断 → corrupt_truncated
    std::fs::write(&path, &bytes[..bytes.len() - 10]).unwrap();
    let (status, body, _) = app.call_fresh(req("GET", "/v1/sets/victim")).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["errors"][0]["code"], "corrupt_truncated");

    // 3) 魔数损坏 → corrupt_bad_magic
    let mut bad = bytes.clone();
    bad[0] = b'X';
    std::fs::write(&path, &bad).unwrap();
    let (status, body, _) = app.call_fresh(req("GET", "/v1/sets/victim")).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["errors"][0]["code"], "corrupt_bad_magic");
}

#[tokio::test]
async fn persistence_survives_fresh_state_reopen() {
    let app = App::new();
    app.call(json_req(
        "POST",
        "/v1/sets?name=p",
        serde_json::json!({"values": [7u32, 8, 90000, u32::MAX]}),
    ))
    .await;
    // 用全新进程状态（无内存缓存）重新打开同一目录
    let (status, body, _) = app.call_fresh(req("GET", "/v1/sets/p")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["result"]["cardinality"], 4);
    assert_eq!(body["result"]["min"], 7);
    assert_eq!(body["result"]["max"], u32::MAX);
}
