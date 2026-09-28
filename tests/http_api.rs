//! HTTP 验证接口端到端测试：在真实 TCP 端口上跑 axum，用 ureq 调用。
//! 断言具体状态码、错误 code、request_id/run_id 头，以及跨请求的版本语义。

mod common;

use std::sync::Arc;
use std::time::Duration;

use common::{TempDir, TestLog};
use pr2d::api::{router, AppState};
use pr2d::store::Store;
use pr2d::telemetry::RunIdentity;
use serde_json::{json, Value};

struct Server {
    addr: String,
    run_id: String,
    _dir: TempDir,
}

fn spawn_server(tag: &str) -> Server {
    let dir = TempDir::new(tag);
    let store = Store::open(dir.path()).unwrap();
    let run = Arc::new(RunIdentity::new());
    let run_id = run.run_id.clone();
    let state = AppState {
        store: Arc::new(store),
        run,
        max_body_bytes: 64 * 1024,
    };
    let app = router(state);

    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        rt.block_on(async move {
            // listener 必须在 runtime 内创建（tokio 1.44 禁止注册外部阻塞 fd）。
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            tx.send(listener.local_addr().unwrap().to_string()).unwrap();
            axum::serve(listener, app).await.unwrap();
        });
    });
    let addr = rx
        .recv_timeout(Duration::from_secs(5))
        .expect("server address");
    Server {
        addr,
        run_id,
        _dir: dir,
    }
}

#[derive(Debug)]
struct Resp {
    status: u16,
    body: Value,
    request_id: String,
    run_id_header: String,
}

fn call(method: &str, url: &str, body: Option<Value>, req_id: Option<&str>) -> Resp {
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(5))
        .build();
    let mut req = agent.request(method, url);
    if let Some(rid) = req_id {
        req = req.set("X-Request-Id", rid);
    }
    let result = match body {
        Some(b) => req.send_string(&b.to_string()),
        None => req.call(),
    };
    let resp = match result {
        Ok(r) => r,
        Err(ureq::Error::Status(_, r)) => r,
        Err(e) => panic!("transport error: {e}"),
    };
    let status = resp.status();
    let request_id = resp.header("x-request-id").unwrap_or("").to_string();
    let run_id_header = resp.header("x-run-id").unwrap_or("").to_string();
    let text = resp.into_string().unwrap_or_default();
    let body = serde_json::from_str(&text).unwrap_or(Value::Null);
    Resp {
        status,
        body,
        request_id,
        run_id_header,
    }
}

fn url(s: &Server, path: &str) -> String {
    format!("http://{}{}", s.addr, path)
}

#[test]
fn http_end_to_end_happy_path_and_identity_headers() {
    let srv = spawn_server("http-happy");
    let mut log = TestLog::new("http_happy");

    // 自定义 request id 必须原样回显；run id 与进程一致
    let r = call(
        "POST",
        &url(&srv, "/v1/tables"),
        Some(json!({"xs":[1,2,2,3],"ys":[10,10,20]})),
        Some("case-http-happy-register"),
    );
    assert_eq!(r.status, 201);
    assert_eq!(r.request_id, "case-http-happy-register");
    assert_eq!(r.run_id_header, srv.run_id);
    log.assert_subset_json(
        "register",
        json!({"xs":[1,2,2,3],"ys":[10,10,20]}),
        json!({"ok":true,"table_id":1,"nx":3,"ny":2,"duplicate_x":1,"duplicate_y":1}),
        r.body,
    );

    // 提交两批
    let r = call(
        "POST",
        &url(&srv, "/v1/tables/1/batches"),
        Some(json!({"updates":[{"x":1,"y":10,"delta":10},{"x":3,"y":20,"delta":-4}]})),
        None,
    );
    assert_eq!(r.status, 200);
    assert_eq!(r.body["version"], 1);
    assert!(r.body["created_at_ms"].as_i64().unwrap() > 0);
    // 未自带 id 时服务端生成，且含 run 身份
    assert!(r.request_id.contains(&srv.run_id));

    let r = call(
        "POST",
        &url(&srv, "/v1/tables/1/batches"),
        Some(json!({"base_version":1,"updates":[{"x":1,"y":10,"delta":5}]})),
        None,
    );
    assert_eq!(r.body["version"], 2);

    // 查询：最新
    let r = call(
        "POST",
        &url(&srv, "/v1/tables/1/query"),
        Some(json!({"x_lo":1,"x_hi":3,"y_lo":10,"y_hi":20})),
        None,
    );
    log.assert_eq_json(
        "query-latest",
        json!({}),
        json!({"ok":true,"version":2,"sum":11,"empty":false}), // 15-4
        json!({"ok":r.body["ok"],"version":r.body["version"],"sum":r.body["sum"],"empty":r.body["empty"]}),
    );
    // 历史版本 v1：10-4=6
    let r = call(
        "POST",
        &url(&srv, "/v1/tables/1/query"),
        Some(json!({"version":1,"x_lo":1,"x_hi":3,"y_lo":10,"y_hi":20})),
        None,
    );
    assert_eq!(r.body["sum"], 6);
    assert_eq!(r.body["version"], 1);
    // 空矩形
    let r = call(
        "POST",
        &url(&srv, "/v1/tables/1/query"),
        Some(json!({"x_lo":4,"x_hi":99,"y_lo":10,"y_hi":20})),
        None,
    );
    assert_eq!(
        (r.status, r.body["sum"].as_i64(), r.body["empty"].as_bool()),
        (200, Some(0), Some(true))
    );

    // 版本清单
    let r = call("GET", &url(&srv, "/v1/tables/1/versions"), None, None);
    assert_eq!(r.status, 200);
    assert_eq!(r.body["versions"].as_array().unwrap().len(), 3);

    // 表元信息：冻结坐标顺序
    let r = call("GET", &url(&srv, "/v1/tables/1"), None, None);
    assert_eq!(r.body["xs"], json!([1, 2, 3]));
    assert_eq!(r.body["ys"], json!([10, 20]));

    // health
    let r = call("GET", &url(&srv, "/health"), None, None);
    assert_eq!(r.status, 200);
    assert_eq!(r.body["run_id"], srv.run_id);
    assert_eq!(r.body["tables"], json!([1]));
}

#[test]
fn http_failure_categories_are_specific() {
    let srv = spawn_server("http-fail");
    let mut log = TestLog::new("http_failures");
    call(
        "POST",
        &url(&srv, "/v1/tables"),
        Some(json!({"xs":[1],"ys":[2]})),
        None,
    );

    let cases: Vec<(&str, Value, u16, &str)> = vec![
        // 非法 JSON
        (
            "POST /batches",
            json!({"updates":"not-a-list"}),
            400,
            "BAD_JSON",
        ),
        // 未知字段
        (
            "POST /batches",
            json!({"updates":[],"extra":1}),
            400,
            "BAD_JSON",
        ),
        // 空批
        ("POST /batches", json!({"updates":[]}), 400, "EMPTY_BATCH"),
        // 未注册坐标
        (
            "POST /batches",
            json!({"updates":[{"x":9,"y":2,"delta":1}]}),
            422,
            "COORDINATE_NOT_REGISTERED",
        ),
        // 倒矩形
        (
            "POST /query",
            json!({"x_lo":9,"x_hi":1,"y_lo":2,"y_hi":2}),
            400,
            "INVERTED_RECT",
        ),
        // 版本不存在
        (
            "POST /query",
            json!({"version":42,"x_lo":1,"x_hi":1,"y_lo":2,"y_hi":2}),
            404,
            "VERSION_NOT_FOUND",
        ),
        // 负版本
        (
            "POST /query",
            json!({"version":-1,"x_lo":1,"x_hi":1,"y_lo":2,"y_hi":2}),
            400,
            "INVALID_REQUEST",
        ),
        // 查询体未知字段（serde flatten 下也必须拒绝）
        (
            "POST /query",
            json!({"x_lo":1,"x_hi":1,"y_lo":2,"y_hi":2,"extra":7}),
            400,
            "BAD_JSON",
        ),
        // 查询体边界拼错（缺 x_lo，不是被当成全域）
        (
            "POST /query",
            json!({"xlo":1,"x_hi":1,"y_lo":2,"y_hi":2}),
            400,
            "BAD_JSON",
        ),
    ];

    for (what, body, want_status, want_code) in cases {
        let path = if what.contains("batches") {
            "/v1/tables/1/batches"
        } else {
            "/v1/tables/1/query"
        };
        let r = call("POST", &url(&srv, path), Some(body.clone()), None);
        log.assert_eq_json(
            what,
            body,
            json!({"status":want_status,"code":want_code,"ok":false}),
            json!({"status":r.status,"code":r.body["error"]["code"],"ok":r.body["ok"]}),
        );
        // 错误信封带身份，绝不返回成功
        assert!(r.body["error"]["request_id"].as_str().is_some());
        assert_eq!(r.body["error"]["run_id"], srv.run_id);
    }

    // 不存在的表 / 非法表 id / 路由 / 方法
    let r = call("GET", &url(&srv, "/v1/tables/99"), None, None);
    assert_eq!(
        (r.status, r.body["error"]["code"].as_str()),
        (404, Some("TABLE_NOT_FOUND"))
    );
    let r = call("GET", &url(&srv, "/v1/tables/0"), None, None);
    assert_eq!(
        (r.status, r.body["error"]["code"].as_str()),
        (400, Some("INVALID_REQUEST"))
    );
    let r = call("GET", &url(&srv, "/nope"), None, None);
    assert_eq!(r.status, 404);
    assert_eq!(r.body["ok"], false);
    let r = call("DELETE", &url(&srv, "/v1/tables/1"), None, None);
    assert_eq!(
        (r.status, r.body["error"]["code"].as_str()),
        (405, Some("METHOD_NOT_ALLOWED"))
    );
}

#[test]
fn http_stale_base_and_body_limit() {
    let srv = spawn_server("http-stale");
    call(
        "POST",
        &url(&srv, "/v1/tables"),
        Some(json!({"xs":[1],"ys":[1]})),
        None,
    );
    call(
        "POST",
        &url(&srv, "/v1/tables/1/batches"),
        Some(json!({"updates":[{"x":1,"y":1,"delta":1}]})),
        None,
    );
    // 当前 v1，基于 v0 → 409
    let r = call(
        "POST",
        &url(&srv, "/v1/tables/1/batches"),
        Some(json!({"base_version":0,"updates":[{"x":1,"y":1,"delta":1}]})),
        None,
    );
    assert_eq!(
        (r.status, r.body["error"]["code"].as_str()),
        (409, Some("STALE_BASE_VERSION"))
    );

    // 请求体超过上限（服务端 64KiB）→ 413 PAYLOAD_TOO_LARGE
    // 直接用原始字符串发送，保证字节数确定（约 40001*2 ≈ 80KB）。
    let big = format!("{{\"xs\":[{}],\"ys\":[1]}}", vec!["1"; 40000].join(","));
    assert!(big.len() > 64 * 1024);
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(5))
        .build();
    let resp = agent
        .post(&url(&srv, "/v1/tables"))
        .set("content-type", "application/json")
        .send_string(&big)
        .unwrap_err();
    let r2 = match resp {
        ureq::Error::Status(code, r) => {
            let body: Value = serde_json::from_str(&r.into_string().unwrap()).unwrap();
            (code, body["error"]["code"].as_str().map(|s| s.to_string()))
        }
        other => panic!("expected status error, got {other:?}"),
    };
    assert_eq!(r2.0, 413);
    assert_eq!(r2.1.as_deref(), Some("PAYLOAD_TOO_LARGE"));
}

#[test]
fn http_restart_persistence() {
    // 关闭后用同一目录重启，数据仍在
    let dir = TempDir::new("http-restart");
    {
        let store = Store::open(dir.path()).unwrap();
        let run = Arc::new(RunIdentity::new());
        let state = AppState {
            store: Arc::new(store),
            run,
            max_body_bytes: 64 * 1024,
        };
        let app = router(state);
        let (tx, rx) = std::sync::mpsc::channel();
        let _handle = std::thread::spawn(move || {
            let rt = tokio::runtime::Runtime::new().unwrap();
            rt.block_on(async move {
                let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
                tx.send(listener.local_addr().unwrap().to_string()).unwrap();
                axum::serve(listener, app).await.unwrap();
            });
        });
        let addr = rx.recv_timeout(Duration::from_secs(5)).unwrap();
        let a = format!("http://{addr}");
        call(
            "POST",
            &format!("{a}/v1/tables"),
            Some(json!({"xs":[5,6],"ys":[7]})),
            None,
        );
        call(
            "POST",
            &format!("{a}/v1/tables/1/batches"),
            Some(json!({"updates":[{"x":5,"y":7,"delta":42}]})),
            None,
        );
        // 数据已 fsync；后台服务线程随测试进程结束。
    }
    // 用文件系统直接重开 Store 验证
    let store = Store::open(dir.path()).unwrap();
    let q = store
        .query(
            1,
            None,
            &pr2d::rect::Rect {
                x_lo: 5,
                x_hi: 5,
                y_lo: 7,
                y_hi: 7,
            },
        )
        .unwrap();
    assert_eq!(q.sum, 42);
}
