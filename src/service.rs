//! Axum HTTP layer: validation endpoints over the FM-index core.
//!
//! Routes:
//! ```text
//! GET    /health
//! POST   /indexes                      create (body: CreateIndex)
//! GET    /indexes                      list
//! GET    /indexes/{name}               metadata
//! DELETE /indexes/{name}               delete
//! POST   /indexes/{name}/search        one or many patterns
//! GET    /indexes/{name}/bwt           inspect BWT / C-table / sampling
//! POST   /indexes/{name}/verify        index vs. exhaustive-scan oracle
//! ```
//! Every response carries `x-run-id`; failures carry a JSON body with
//! `category` and `code` so input errors, state conflicts, resource
//! exhaustion and compute failures are distinguishable by callers.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use axum::{
    Extension, Json, Router,
    body::Body,
    extract::{Path as UrlPath, State},
    http::{HeaderMap, Method, StatusCode, header},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use base64::Engine;
use serde::{Deserialize, Serialize};
use tower::ServiceExt;

use crate::config::Config;
use crate::error::Error;
use crate::fm::{FmIndex, SearchOutcome};
use crate::logging::{RequestLog, RequestRecord};
use crate::now_ms;
use crate::persistence::Catalog;
use crate::reference::{naive_scan, text_stats};

#[derive(Clone)]
pub struct AppState {
    catalog: Arc<Mutex<Catalog>>,
    cache: Arc<Mutex<HashMap<String, Arc<FmIndex>>>>,
    cfg: Arc<Config>,
    logs: Arc<RequestLog>,
}

impl AppState {
    pub fn new(cfg: Config) -> Result<Self, Error> {
        let catalog = Catalog::open(&cfg.data_dir)?;
        let logs = Arc::new(RequestLog::new(cfg.log_dir.as_deref()));
        Ok(Self {
            catalog: Arc::new(Mutex::new(catalog)),
            cache: Arc::new(Mutex::new(HashMap::new())),
            cfg: Arc::new(cfg),
            logs,
        })
    }

    /// Variant honoring `FM_LOG_DIR` (used by the binary).
    pub fn with_request_log(self, log: RequestLog) -> Self {
        Self {
            logs: Arc::new(log),
            ..self
        }
    }

    fn cached_index(&self, name: &str) -> Result<Arc<FmIndex>, Error> {
        if let Some(idx) = self.cache.lock().poison()?.get(name) {
            return Ok(idx.clone());
        }
        let idx = {
            let cat = self.catalog.lock().poison()?;
            cat.open_index(name)?
        };
        let arc = Arc::new(idx);
        self.cache
            .lock()
            .poison()?
            .insert(name.to_string(), arc.clone());
        Ok(arc)
    }

    fn evict(&self, name: &str) -> Result<(), Error> {
        self.cache.lock().poison()?.remove(name);
        Ok(())
    }
}

/// A poisoned service mutex surfaces as a compute failure, not a panic.
trait PoisonExt<T> {
    fn poison(self) -> Result<T, Error>;
}
impl<T> PoisonExt<T> for std::sync::LockResult<T> {
    fn poison(self) -> Result<T, Error> {
        self.map_err(|p| Error::Invariant(format!("internal mutex poisoned: {p}")))
    }
}

// ---------------------------------------------------------------------------
// DTOs
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct CreateIndex {
    pub name: Option<String>,
    /// Text encoded per `encoding` ("b64" standard base64, or "hex").
    pub text: Option<String>,
    pub encoding: Option<String>,
    pub sample_interval: Option<u32>,
}

#[derive(Debug, Deserialize)]
pub struct QueryBody {
    pub pattern: Option<String>,
    pub patterns: Option<Vec<String>>,
    pub encoding: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct MetaOut {
    pub name: String,
    pub text_len: u64,
    pub sample_interval: u32,
    pub sha256: String,
    pub created_unix_ms: u128,
}

#[derive(Debug, Serialize)]
pub struct SearchResultOut {
    pub pattern_index: usize,
    pub lo: u64,
    pub hi: u64,
    pub count: u64,
    pub empty_pattern: bool,
    pub note: String,
    pub positions: Vec<u64>,
}

#[derive(Debug, Serialize)]
pub struct SearchOut {
    pub run_id: String,
    pub index: String,
    pub text_len: u64,
    pub results: Vec<SearchResultOut>,
}

#[derive(Debug, Serialize)]
pub struct VerifyResult {
    pub pattern_index: usize,
    pub lo: u64,
    pub hi: u64,
    pub index_count: u64,
    pub scan_count: u64,
    pub index_positions: Vec<u64>,
    pub scan_positions: Vec<u64>,
    pub agree: bool,
    pub reason: String,
}

#[derive(Debug, Serialize)]
pub struct VerifyOut {
    pub run_id: String,
    pub index: String,
    pub text_len: u64,
    pub zero_bytes: u64,
    pub distinct_bytes: u64,
    pub all_agree: bool,
    pub results: Vec<VerifyResult>,
}

#[derive(Debug, Serialize)]
struct ErrorBody<'a> {
    category: &'static str,
    code: &'static str,
    message: String,
    run_id: &'a str,
}

// ---------------------------------------------------------------------------
// Decoding / response helpers
// ---------------------------------------------------------------------------

fn decode_payload(s: &str, encoding: &Option<String>) -> Result<Vec<u8>, Error> {
    match encoding.as_deref().unwrap_or("b64") {
        "b64" => base64::engine::general_purpose::STANDARD
            .decode(s)
            .map_err(|e| Error::BadBase64(e.to_string())),
        "hex" => decode_hex(s),
        other => Err(Error::BadEncoding(other.to_string())),
    }
}

fn decode_hex(s: &str) -> Result<Vec<u8>, Error> {
    let b = s.as_bytes();
    if !b.len().is_multiple_of(2) {
        return Err(Error::BadBase64("hex input has odd length".into()));
    }
    let mut out = Vec::with_capacity(b.len() / 2);
    let nib = |c: u8| -> Result<u8, Error> {
        match c {
            b'0'..=b'9' => Ok(c - b'0'),
            b'a'..=b'f' => Ok(c - b'a' + 10),
            b'A'..=b'F' => Ok(c - b'A' + 10),
            _ => Err(Error::BadBase64("hex input has non-hex digit".into())),
        }
    };
    let (pairs, rest) = b.as_chunks::<2>();
    debug_assert!(rest.is_empty());
    for pair in pairs {
        out.push((nib(pair[0])? << 4) | nib(pair[1])?);
    }
    Ok(out)
}

struct Ctx {
    run_id: String,
    start: Instant,
    index: Option<String>,
}

impl Ctx {
    fn new(run_id: &str, index: Option<String>) -> Self {
        Self {
            run_id: run_id.to_string(),
            start: Instant::now(),
            index,
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn finish(
        self,
        state: &AppState,
        method: Method,
        path: String,
        status: u16,
        interval: Option<(u64, u64)>,
        hit_count: Option<u64>,
        patterns: Option<usize>,
        err: Option<&Error>,
    ) {
        let rec = RequestRecord {
            run_id: self.run_id,
            ts_unix_ms: now_ms(),
            method: method.to_string(),
            path,
            status,
            duration_ms: self.start.elapsed().as_millis(),
            index: self.index,
            interval,
            hit_count,
            patterns,
            error_category: err.map(|e| e.category().as_str()),
            error_code: err.map(Error::code),
        };
        state.logs.record(&rec);
    }
}

fn fail_response(run_id: &str, err: &Error) -> Response {
    tracing::warn!(
        run_id = %run_id, error = %err,
        category = err.category().as_str(), code = err.code(),
        "request failed"
    );
    let status =
        StatusCode::from_u16(err.http_status()).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    let body = ErrorBody {
        category: err.category().as_str(),
        code: err.code(),
        message: err.to_string(),
        run_id,
    };
    let mut resp = (status, Json(body)).into_response();
    insert_run_header(resp.headers_mut(), run_id);
    resp
}

fn ok_response(run_id: &str, value: impl Serialize) -> Response {
    let mut resp = Json(value).into_response();
    insert_run_header(resp.headers_mut(), run_id);
    resp
}

fn insert_run_header(headers: &mut HeaderMap, run_id: &str) {
    if let Ok(v) = header::HeaderValue::from_str(run_id) {
        headers.insert("x-run-id", v);
    }
}

// ---------------------------------------------------------------------------
// Run-id middleware (client may supply one; otherwise a UUID is generated)
// ---------------------------------------------------------------------------

#[derive(Clone)]
struct RunId(pub String);

async fn run_id_mw(mut req: axum::http::Request<Body>, next: Next) -> Response {
    let run_id = req
        .headers()
        .get("x-run-id")
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    req.extensions_mut().insert(RunId(run_id.clone()));
    let mut resp = next.run(req).await;
    if let Ok(v) = header::HeaderValue::from_str(&run_id) {
        resp.headers_mut().insert("x-run-id", v);
    }
    resp
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/indexes", get(list_indexes).post(create_index))
        .route("/indexes/{name}", get(get_index).delete(delete_index))
        .route("/indexes/{name}/search", post(search))
        .route("/indexes/{name}/bwt", get(bwt_inspect))
        .route("/indexes/{name}/verify", post(verify))
        .with_state(state)
        .layer(middleware::from_fn(run_id_mw))
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok", "service": "fm-index" }))
}

async fn create_index(
    State(state): State<AppState>,
    Extension(RunId(run_id)): Extension<RunId>,
    Json(body): Json<CreateIndex>,
) -> Response {
    let ctx = Ctx::new(&run_id, body.name.clone());
    let result = (|| -> Result<_, Error> {
        let name = body.name.ok_or(Error::MissingField("name"))?;
        crate::persistence::validate_name(&name)?;
        let text_encoded = body.text.ok_or(Error::MissingField("text"))?;
        let text = decode_payload(&text_encoded, &body.encoding)?;
        if text.is_empty() {
            return Err(Error::EmptyText);
        }
        if text.len() > state.cfg.max_text_bytes {
            return Err(Error::TextTooLarge {
                size: text.len(),
                limit: state.cfg.max_text_bytes,
            });
        }
        let k = body
            .sample_interval
            .unwrap_or(state.cfg.default_sample_interval);
        let meta = {
            let mut cat = state.catalog.lock().poison()?;
            cat.create(&name, text, k)?
        };
        state.evict(&name)?;
        Ok(meta)
    })();

    match result {
        Ok(meta) => {
            ctx.finish(
                &state,
                Method::POST,
                "/indexes".into(),
                201,
                None,
                None,
                None,
                None,
            );
            let out = MetaOut {
                name: meta.name,
                text_len: meta.text_len,
                sample_interval: meta.sample_interval,
                sha256: meta.sha256,
                created_unix_ms: meta.created_unix_ms,
            };
            let mut resp = ok_response(&run_id, out);
            *resp.status_mut() = StatusCode::CREATED;
            resp
        }
        Err(e) => {
            ctx.finish(
                &state,
                Method::POST,
                "/indexes".into(),
                e.http_status(),
                None,
                None,
                None,
                Some(&e),
            );
            fail_response(&run_id, &e)
        }
    }
}

async fn list_indexes(
    State(state): State<AppState>,
    Extension(RunId(run_id)): Extension<RunId>,
) -> Response {
    let metas = match state.catalog.lock().poison() {
        Ok(c) => c.list().into_iter().cloned().collect::<Vec<_>>(),
        Err(e) => return fail_response(&run_id, &e),
    };
    let out: Vec<MetaOut> = metas
        .into_iter()
        .map(|m| MetaOut {
            name: m.name,
            text_len: m.text_len,
            sample_interval: m.sample_interval,
            sha256: m.sha256,
            created_unix_ms: m.created_unix_ms,
        })
        .collect();
    ok_response(
        &run_id,
        serde_json::json!({ "run_id": run_id, "indexes": out }),
    )
}

async fn get_index(
    State(state): State<AppState>,
    Extension(RunId(run_id)): Extension<RunId>,
    UrlPath(name): UrlPath<String>,
) -> Response {
    let meta = state.catalog.lock().poison().and_then(|c| {
        c.meta(&name)
            .cloned()
            .ok_or_else(|| Error::NotFound(name.clone()))
    });
    match meta {
        Ok(m) => ok_response(&run_id, m),
        Err(e) => fail_response(&run_id, &e),
    }
}

async fn delete_index(
    State(state): State<AppState>,
    Extension(RunId(run_id)): Extension<RunId>,
    UrlPath(name): UrlPath<String>,
) -> Response {
    let result = {
        let mut cat = match state.catalog.lock().poison() {
            Ok(c) => c,
            Err(e) => return fail_response(&run_id, &e),
        };
        cat.delete(&name)
    };
    match result {
        Ok(meta) => {
            let _ = state.evict(&name);
            ok_response(
                &run_id,
                serde_json::json!({
                    "run_id": run_id, "deleted": meta.name, "text_len": meta.text_len
                }),
            )
        }
        Err(e) => fail_response(&run_id, &e),
    }
}

async fn search(
    State(state): State<AppState>,
    Extension(RunId(run_id)): Extension<RunId>,
    UrlPath(name): UrlPath<String>,
    Json(body): Json<QueryBody>,
) -> Response {
    let ctx = Ctx::new(&run_id, Some(name.clone()));
    let result = (|| -> Result<_, Error> {
        let idx = state.cached_index(&name)?;
        let encoded = decode_pattern_list(&body)?;
        let mut results = Vec::with_capacity(encoded.len());
        let mut last_interval = (0u64, 0u64);
        let mut total_hits = 0u64;
        for (i, raw) in encoded.iter().enumerate() {
            let pattern = decode_payload(raw, &body.encoding)?;
            let out = idx.search(&pattern);
            last_interval = (out.lo, out.hi);
            total_hits += out.count;
            let note = edge_note(&pattern, idx.text().len(), &out);
            results.push(SearchResultOut {
                pattern_index: i,
                lo: out.lo,
                hi: out.hi,
                count: out.count,
                empty_pattern: out.empty_pattern,
                note,
                positions: out.positions,
            });
        }
        Ok((idx, results, last_interval, total_hits, encoded.len()))
    })();

    match result {
        Ok((idx, results, interval, total_hits, n)) => {
            ctx.finish(
                &state,
                Method::POST,
                format!("/indexes/{name}/search"),
                200,
                Some(interval),
                Some(total_hits),
                Some(n),
                None,
            );
            ok_response(
                &run_id,
                SearchOut {
                    run_id: run_id.clone(),
                    index: name,
                    text_len: idx.text_len(),
                    results,
                },
            )
        }
        Err(e) => {
            ctx.finish(
                &state,
                Method::POST,
                format!("/indexes/{name}/search"),
                e.http_status(),
                None,
                None,
                None,
                Some(&e),
            );
            fail_response(&run_id, &e)
        }
    }
}

async fn bwt_inspect(
    State(state): State<AppState>,
    Extension(RunId(run_id)): Extension<RunId>,
    UrlPath(name): UrlPath<String>,
) -> Response {
    let idx = match state.cached_index(&name) {
        Ok(idx) => idx,
        Err(e) => return fail_response(&run_id, &e),
    };
    let bwt: Vec<u16> = idx.bwt().to_vec();
    let sentinel_row = bwt
        .iter()
        .position(|&s| s == crate::coding::SENTINEL)
        .map(|p| p as u64);
    let c_nonzero: Vec<(u16, u64)> = idx
        .c_table()
        .iter()
        .enumerate()
        .filter(|(s, _)| idx.occ_table().total(*s as u16) > 0)
        .map(|(s, v)| (s as u16, *v))
        .collect();
    ok_response(
        &run_id,
        serde_json::json!({
            "run_id": run_id,
            "index": name,
            "coded_len": idx.coded_len(),
            "text_len": idx.text_len(),
            "sample_interval": idx.sample_interval(),
            "sentinel_symbol": 0,
            "sentinel_bwt_row": sentinel_row,
            "c_table_nonzero": c_nonzero,
            "sa_sample_count": idx.sa_samples().len(),
            "bwt_symbols": bwt,
        }),
    )
}

async fn verify(
    State(state): State<AppState>,
    Extension(RunId(run_id)): Extension<RunId>,
    UrlPath(name): UrlPath<String>,
    Json(body): Json<QueryBody>,
) -> Response {
    let result = (|| -> Result<_, Error> {
        let idx = state.cached_index(&name)?;
        let encoded = body
            .patterns
            .clone()
            .ok_or(Error::MissingField("patterns"))?;
        if encoded.is_empty() {
            return Err(Error::MissingField("patterns must be non-empty"));
        }
        let mut results = Vec::with_capacity(encoded.len());
        let mut all_agree = true;
        for (i, raw) in encoded.iter().enumerate() {
            let pattern = decode_payload(raw, &body.encoding)?;
            let out = idx.search(&pattern);
            let scan = naive_scan(idx.text(), &pattern);
            let agree = out.positions == scan;
            all_agree &= agree;
            let reason = if !agree {
                format!(
                    "MISMATCH: backwards search interval [{},{}) -> {} located positions, \
                     exhaustive scan found {}",
                    out.lo,
                    out.hi,
                    out.positions.len(),
                    scan.len()
                )
            } else {
                format!(
                    "agree: interval [{},{}) ({} match{}); scan confirms {} position{}",
                    out.lo,
                    out.hi,
                    out.count,
                    if out.count == 1 { "" } else { "es" },
                    scan.len(),
                    if scan.len() == 1 { "" } else { "s" }
                )
            };
            results.push(VerifyResult {
                pattern_index: i,
                lo: out.lo,
                hi: out.hi,
                index_count: out.count,
                scan_count: scan.len() as u64,
                index_positions: out.positions,
                scan_positions: scan,
                agree,
                reason,
            });
        }
        Ok((idx, results, all_agree, encoded.len()))
    })();

    match result {
        Ok((idx, results, all_agree, n)) => {
            let stats = text_stats(idx.text());
            let ctx = Ctx::new(&run_id, Some(name.clone()));
            ctx.finish(
                &state,
                Method::POST,
                format!("/indexes/{name}/verify"),
                200,
                None,
                None,
                Some(n),
                None,
            );
            ok_response(
                &run_id,
                VerifyOut {
                    run_id: run_id.clone(),
                    index: name,
                    text_len: stats.length,
                    zero_bytes: stats.zero_bytes,
                    distinct_bytes: stats.distinct_bytes,
                    all_agree,
                    results,
                },
            )
        }
        Err(e) => fail_response(&run_id, &e),
    }
}

fn decode_pattern_list(body: &QueryBody) -> Result<Vec<String>, Error> {
    match (&body.pattern, &body.patterns) {
        (Some(s), None) => Ok(vec![s.clone()]),
        (None, Some(v)) if !v.is_empty() => Ok(v.clone()),
        (None, Some(_)) => Err(Error::MissingField("patterns must be non-empty")),
        (Some(_), Some(_)) => Err(Error::MissingField("provide pattern or patterns, not both")),
        (None, None) => Err(Error::MissingField("pattern")),
    }
}

fn edge_note(pattern: &[u8], text_len: usize, out: &SearchOutcome) -> String {
    if out.empty_pattern {
        "empty pattern: matches every suffix boundary 0..=text_len".to_string()
    } else if pattern.len() > text_len {
        "pattern longer than text: empty interval by definition".to_string()
    } else if !out.is_match() {
        "backwards search collapsed to an empty interval".to_string()
    } else {
        "half-open interval [lo,hi) localized via LF + SA samples".to_string()
    }
}

/// Bind and serve (used by the binary).
pub async fn serve(state: AppState, bind: &str) -> Result<(), Error> {
    let listener = tokio::net::TcpListener::bind(bind).await?;
    tracing::info!(%bind, "FM-index service listening");
    axum::serve(listener, router(state)).await?;
    Ok(())
}

/// One-shot request helper for integration tests.
pub async fn oneshot(state: AppState, req: axum::http::Request<Body>) -> Response {
    router(state)
        .oneshot(req)
        .await
        .expect("router never fails")
}
