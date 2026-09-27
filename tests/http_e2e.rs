//! HTTP 端到端测试：用真实 TCP 绑定启动服务，通过 HTTP 请求验证
//! 状态码、错误码、请求/运行关联头与端到端插删查语义。

use std::process::Command;
use std::time::{Duration, Instant};

use serde_json::Value;
use tempfile::TempDir;

fn bin() -> std::path::PathBuf {
    // 由 `cargo test` 构建出的 cf-svc 二进制（与测试同 target 目录）。
    let p = std::path::PathBuf::from(env!("CARGO_BIN_EXE_cf-svc"));
    assert!(p.exists(), "cf-svc 二进制应已构建");
    p
}

struct RunningServer {
    child: std::process::Child,
    base: String,
    _dir: TempDir,
    run_id: String,
}

impl Drop for RunningServer {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn write_config(dir: &std::path::Path, port: u16) {
    let cfg = format!(
        r#"
[server]
host = "127.0.0.1"
port = {port}
max_request_bytes = 1048576

[filter]
num_buckets = 256
bucket_size = 4
fingerprint_bits = 12
max_kicks = 100
seed_hex = "1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff"

[storage]
data_dir = "{data}"
snapshot_file = "snapshot.bin"
fsync = false

[credentials]
hmac_secret_hex = "aaaaaaaaaaaaaaaabbbbbbbbbbbbbbbbccccccccccccccccdddddddddddddddd"

[logging]
level = "debug"
format = "text"
run_id = "e2e-run-fixed"
"#,
        data = dir.join("data").display()
    );
    let p = dir.join("config.toml");
    std::fs::create_dir_all(dir).unwrap();
    std::fs::write(p, cfg).unwrap();
}

fn start_server() -> RunningServer {
    let dir = tempfile::tempdir().unwrap();
    let port = pick_port();
    write_config(dir.path(), port);
    let cfg_path = dir.path().join("config.toml");
    let child = Command::new(bin())
        .arg(&cfg_path)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("启动 cf-svc");

    let base = format!("http://127.0.0.1:{port}");
    wait_ready(&base);
    RunningServer {
        child,
        base,
        _dir: dir,
        run_id: "e2e-run-fixed".into(),
    }
}

fn pick_port() -> u16 {
    use std::net::TcpListener;
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    l.local_addr().unwrap().port()
}

fn wait_ready(base: &str) {
    let start = Instant::now();
    loop {
        if let Ok(out) = http(&format!("{base}/healthz"), "GET", None, None) {
            if out.status == 200 {
                return;
            }
        }
        assert!(
            start.elapsed() < Duration::from_secs(10),
            "服务未在 10s 内就绪"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
}

struct Resp {
    status: u16,
    body: Value,
    request_id: String,
    run_id: String,
}

fn http(
    url: &str,
    method: &str,
    json_body: Option<&str>,
    req_id: Option<&str>,
) -> std::io::Result<Resp> {
    use std::sync::atomic::{AtomicU64, Ordering};
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let uniq = SEQ.fetch_add(1, Ordering::Relaxed);
    let body_path = format!("/tmp/cf_body_{}_{uniq}", std::process::id());
    let hdr_path = format!("/tmp/cf_headers_{}_{uniq}", std::process::id());

    let mut cmd = Command::new("curl");
    cmd.arg("-sS")
        .arg("-o")
        .arg(&body_path)
        .arg("-w")
        .arg("%{http_code}");
    cmd.arg("-X").arg(method);
    if let Some(b) = json_body {
        cmd.arg("-H")
            .arg("Content-Type: application/json")
            .arg("--data")
            .arg(b);
    }
    if let Some(r) = req_id {
        cmd.arg("-H").arg(format!("x-request-id: {r}"));
    }
    cmd.arg("-D").arg(&hdr_path);
    cmd.arg(url);
    let out = cmd.output()?;
    let status = String::from_utf8_lossy(&out.stdout)
        .trim()
        .parse::<u16>()
        .unwrap_or(0);
    let body_text = std::fs::read_to_string(&body_path).unwrap_or_default();
    let headers = std::fs::read_to_string(&hdr_path).unwrap_or_default();
    let _ = std::fs::remove_file(&body_path);
    let _ = std::fs::remove_file(&hdr_path);
    let body: Value = serde_json::from_str(&body_text).unwrap_or(Value::Null);
    let get_header = |name: &str| {
        headers
            .lines()
            .find(|l| l.to_lowercase().starts_with(&name.to_lowercase()))
            .and_then(|l| l.split(':').nth(1))
            .map(|s| s.trim().to_string())
            .unwrap_or_default()
    };
    Ok(Resp {
        status,
        body,
        request_id: get_header("x-request-id"),
        run_id: get_header("x-run-id"),
    })
}

#[test]
fn health_reports_version_and_run_identity() {
    let s = start_server();
    let r = http(&format!("{}/healthz", s.base), "GET", None, None).unwrap();
    assert_eq!(r.status, 200);
    assert_eq!(r.body["status"], "serving");
    assert_eq!(r.body["version"], env!("CARGO_PKG_VERSION"));
    assert_eq!(r.run_id, s.run_id, "响应头必须回显运行身份");
    assert!(!r.request_id.is_empty(), "必须生成请求标识");
}

#[test]
fn end_to_end_insert_query_delete_replay_and_restart() {
    let s = start_server();
    let base = s.base.clone();

    // 自定义请求标识必须被回显（输入↔运行身份关联）。
    let r = http(
        &format!("{base}/filter/insert"),
        "POST",
        Some(r#"{"key":"e2e:user:42"}"#),
        Some("my-correlation-1"),
    )
    .unwrap();
    assert_eq!(r.status, 200, "插入应 200，body={}", r.body);
    assert_eq!(r.request_id, "my-correlation-1");
    assert_eq!(r.body["newly_occupied"], true);
    assert_eq!(r.body["duplicate"], false);
    assert_eq!(r.body["live_count"], 1);
    let token1 = r.body["delete_token"].as_str().unwrap().to_string();

    // 查询命中。
    let r = http(
        &format!("{base}/filter/contains"),
        "POST",
        Some(r#"{"key":"e2e:user:42"}"#),
        None,
    )
    .unwrap();
    assert_eq!(r.status, 200);
    assert_eq!(r.body["member"], true);

    // 重复插入：duplicate=true，live_count=2，新令牌不同。
    let r = http(
        &format!("{base}/filter/insert"),
        "POST",
        Some(r#"{"key":"e2e:user:42"}"#),
        None,
    )
    .unwrap();
    assert_eq!(r.status, 200);
    assert_eq!(r.body["duplicate"], true);
    assert_eq!(r.body["live_count"], 2);
    let token2 = r.body["delete_token"].as_str().unwrap().to_string();
    assert_ne!(token1, token2, "重复插入必须签发不同序号的令牌");

    // 未插入键大概率为 false（12 位指纹 256 桶，单键假阳性概率约 1/256，重试几个不同键）。
    let mut any_absent = false;
    for n in 0..8 {
        let r = http(
            &format!("{base}/filter/contains"),
            "POST",
            Some(&format!(r#"{{"key":"definitely-not-here-{n}"}}"#)),
            None,
        )
        .unwrap();
        if r.body["member"] == false {
            any_absent = true;
            break;
        }
    }
    assert!(
        any_absent,
        "未插入键应能观察到 member=false（允许偶发假阳性）"
    );

    // 用第一张令牌删除：live_count 2->1，键仍命中。
    let r = http(
        &format!("{base}/filter/delete"),
        "POST",
        Some(&format!(r#"{{"delete_token":"{token1}"}}"#)),
        None,
    )
    .unwrap();
    assert_eq!(r.status, 200, "{}", r.body);
    assert_eq!(r.body["live_count"], 1);
    let r = http(
        &format!("{base}/filter/contains"),
        "POST",
        Some(r#"{"key":"e2e:user:42"}"#),
        None,
    )
    .unwrap();
    assert_eq!(r.body["member"], true);

    // 重放同一张令牌：403 token_replayed，绝不是成功。
    let r = http(
        &format!("{base}/filter/delete"),
        "POST",
        Some(&format!(r#"{{"delete_token":"{token1}"}}"#)),
        None,
    )
    .unwrap();
    assert_eq!(r.status, 403);
    assert_eq!(r.body["ok"], false);
    assert_eq!(r.body["error"]["code"], "token_replayed");
    assert!(!r.body["error"]["request_id"].as_str().unwrap().is_empty());

    // 第二张令牌删除最后一份：live_count=0。
    let r = http(
        &format!("{base}/filter/delete"),
        "POST",
        Some(&format!(r#"{{"delete_token":"{token2}"}}"#)),
        None,
    )
    .unwrap();
    assert_eq!(r.status, 200);
    assert_eq!(r.body["live_count"], 0);

    // 伪造令牌：403 bad_signature。
    let r = http(
        &format!("{base}/filter/delete"),
        "POST",
        Some(r#"{"delete_token":"aGVsbG9sb3JlbXl0b2tlbmFuZHBheWxvYWRoZXJl"}"#),
        None,
    )
    .unwrap();
    // 该 base64 解码后长度不足 -> malformed_token；再测一个签名错误。
    assert!(
        r.body["error"]["code"] == "token_malformed"
            || r.body["error"]["code"] == "token_bad_signature"
    );

    // 统计端点反映计数。
    let r = http(&format!("{base}/stats"), "GET", None, None).unwrap();
    assert_eq!(r.status, 200);
    assert_eq!(r.body["stats"]["counters"]["insert_ok"], 1);
    assert_eq!(r.body["stats"]["counters"]["insert_dup"], 1);
    assert_eq!(r.body["stats"]["counters"]["delete_ok"], 2);
    assert!(
        r.body["stats"]["counters"]["delete_denied"]
            .as_u64()
            .unwrap()
            >= 2
    );
}

#[test]
fn bad_requests_have_explicit_codes_not_success() {
    let s = start_server();
    let base = s.base.clone();

    // 空键。
    let r = http(
        &format!("{base}/filter/contains"),
        "POST",
        Some(r#"{"key":""}"#),
        None,
    )
    .unwrap();
    assert_eq!(r.status, 400);
    assert_eq!(r.body["error"]["code"], "key_empty");

    // 非 JSON。
    let r = http(
        &format!("{base}/filter/contains"),
        "POST",
        Some("not-json"),
        None,
    )
    .unwrap();
    assert_eq!(r.status, 400);
    assert_eq!(r.body["error"]["code"], "invalid_request");

    // 错误 content-type 缺失也拒绝。
    let mut cmd = std::process::Command::new("curl");
    cmd.arg("-sS")
        .arg("-o")
        .arg("/tmp/cf_body2")
        .arg("-w")
        .arg("%{http_code}")
        .arg("-X")
        .arg("POST")
        .arg("--data")
        .arg("{}")
        .arg(format!("{base}/filter/contains"));
    let out = cmd.output().unwrap();
    assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), "400");

    // 未知路由 404。
    let r = http(&format!("{base}/nope"), "GET", None, None).unwrap();
    assert_eq!(r.status, 404);
    assert_eq!(r.body["error"]["code"], "not_found");
}

#[test]
fn state_survives_server_restart() {
    let dir_for_cfg = tempfile::tempdir().unwrap();
    let port = pick_port();
    write_config(dir_for_cfg.path(), port);
    let cfg_path = dir_for_cfg.path().join("config.toml");
    let base = format!("http://127.0.0.1:{port}");

    let mut child = Command::new(bin()).arg(&cfg_path).spawn().unwrap();
    wait_ready(&base);
    let r = http(
        &format!("{base}/filter/insert"),
        "POST",
        Some(r#"{"key":"persist-me"}"#),
        None,
    )
    .unwrap();
    assert_eq!(r.status, 200);
    let token = r.body["delete_token"].as_str().unwrap().to_string();
    child.kill().unwrap();
    child.wait().unwrap();

    // 重启：状态与令牌应保留。
    let mut child2 = Command::new(bin()).arg(&cfg_path).spawn().unwrap();
    wait_ready(&base);
    let r = http(
        &format!("{base}/filter/contains"),
        "POST",
        Some(r#"{"key":"persist-me"}"#),
        None,
    )
    .unwrap();
    assert_eq!(r.body["member"], true, "重启后存活键不得假阴性");
    let r = http(
        &format!("{base}/filter/delete"),
        "POST",
        Some(&format!(r#"{{"delete_token":"{token}"}}"#)),
        None,
    )
    .unwrap();
    assert_eq!(r.status, 200, "旧令牌重启后仍有效：{}", r.body);
    child2.kill().unwrap();
    child2.wait().unwrap();
}
