#![allow(dead_code)]
//! 测试共享工具：
//! - [`RunLog`]：为每个用例生成可重放运行编号，打印关键中间状态与判定理由；
//! - [`check_assignment`]：**独立于求解内核**的赋值回代校验器；
//! - HTTP 助手：oneshot 驱动 axum 路由，返回状态码/头/JSON。

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};

use axum::body::Body;
use axum::http::{header, HeaderMap, Request, StatusCode};
use axum::Router;
use diff_constraints_service::model::ConstraintInput;
use http_body_util::BodyExt;
use serde_json::Value;
use tower::ServiceExt;

static RUN_COUNTER: AtomicU64 = AtomicU64::new(0);

/// 可重放的用例运行编号（`t-runNNN`）。所有日志行带同一编号，便于重放。
pub struct RunLog {
    pub id: String,
    pub case: String,
}

impl RunLog {
    pub fn new(case: &str) -> Self {
        let n = RUN_COUNTER.fetch_add(1, Ordering::Relaxed) + 1;
        let id = format!("t-run{n:03}");
        eprintln!("[{id}] BEGIN case={case}");
        RunLog {
            id,
            case: case.to_string(),
        }
    }

    /// 记录一个关键中间状态。
    pub fn state(&self, key: &str, value: impl std::fmt::Debug) {
        eprintln!("[{}] state {} = {:?}", self.id, key, value);
    }

    /// 记录最终判定与理由（reason 必须说明“为什么”，不能只说“调用成功”）。
    pub fn verdict(&self, outcome: &str, reason: &str) {
        eprintln!(
            "[{}] END case={} verdict={} reason={}",
            self.id, self.case, outcome, reason
        );
    }
}

/// 构造一条约束的小工具。
pub fn c(name: &str, x: &str, y: &str, k: i64) -> ConstraintInput {
    ConstraintInput {
        name: name.to_string(),
        x: x.to_string(),
        y: y.to_string(),
        c: k,
    }
}

/// 约束 DTO -> 内核行（name, x, y, c）。
pub fn rows(cs: &[ConstraintInput]) -> Vec<(String, String, String, i64)> {
    cs.iter()
        .map(|c| (c.name.clone(), c.x.clone(), c.y.clone(), c.c))
        .collect()
}

/// **独立**可行性校验：把赋值逐条代回 `x - y <= c`，不经过任何求解器代码。
///
/// 这是“参考答案不由被测核心自身生成”的保证之一：
/// 参考答案的正确性由这个朴素回代器（及各用例中手算的具体数值）背书。
pub fn check_assignment(
    constraints: &[ConstraintInput],
    assignment: &BTreeMap<String, i64>,
) -> Result<(), String> {
    let mut vars = std::collections::BTreeSet::new();
    for k in constraints {
        vars.insert(k.x.as_str());
        vars.insert(k.y.as_str());
    }
    for v in vars {
        if !assignment.contains_key(v) {
            return Err(format!("variable {v:?} is missing from the assignment"));
        }
    }
    for k in constraints {
        let x = assignment[&k.x];
        let y = assignment[&k.y];
        let diff = x
            .checked_sub(y)
            .ok_or_else(|| format!("x - y overflowed while checking constraint {}", k.name))?;
        if diff > k.c {
            return Err(format!(
                "constraint {} violated: {} - {} = {} > {}",
                k.name, k.x, k.y, diff, k.c
            ));
        }
    }
    Ok(())
}

/// 可复现的简易确定性 LCG（随机化交叉测试用，种子写死保证可重放）。
pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Self {
        Rng(seed)
    }
    pub fn next_u64(&mut self) -> u64 {
        // MMIX LCG
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0
    }
    pub fn range(&mut self, lo: i64, hi_exclusive: i64) -> i64 {
        let span = (hi_exclusive - lo) as u64;
        lo + (self.next_u64() % span) as i64
    }
}

// ---------- HTTP 助手 ----------

async fn send(
    app: &Router,
    method: &str,
    path: &str,
    content_type: Option<&str>,
    run_header: Option<&str>,
    body: Vec<u8>,
) -> (StatusCode, HeaderMap, Value) {
    let mut builder = Request::builder().method(method).uri(path);
    if let Some(ct) = content_type {
        builder = builder.header(header::CONTENT_TYPE, ct);
    }
    if let Some(rid) = run_header {
        builder = builder.header("x-run-id", rid);
    }
    let request = builder.body(Body::from(body)).expect("valid request");
    let response = app.clone().oneshot(request).await.expect("router response");
    let status = response.status();
    let headers = response.headers().clone();
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("read body")
        .to_bytes();
    let json = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, headers, json)
}

pub async fn post_json(app: &Router, path: &str, body: &Value) -> (StatusCode, HeaderMap, Value) {
    send(
        app,
        "POST",
        path,
        Some("application/json"),
        None,
        serde_json::to_vec(body).unwrap(),
    )
    .await
}

pub async fn post_json_with_run(
    app: &Router,
    path: &str,
    body: &Value,
    run_id: &str,
) -> (StatusCode, HeaderMap, Value) {
    send(
        app,
        "POST",
        path,
        Some("application/json"),
        Some(run_id),
        serde_json::to_vec(body).unwrap(),
    )
    .await
}

pub async fn post_raw(
    app: &Router,
    path: &str,
    content_type: Option<&str>,
    body: Vec<u8>,
) -> (StatusCode, HeaderMap, Value) {
    send(app, "POST", path, content_type, None, body).await
}

pub async fn get_json(app: &Router, path: &str) -> (StatusCode, HeaderMap, Value) {
    send(app, "GET", path, None, None, Vec::new()).await
}

pub async fn delete(app: &Router, path: &str) -> (StatusCode, HeaderMap, Value) {
    send(app, "DELETE", path, None, None, Vec::new()).await
}
