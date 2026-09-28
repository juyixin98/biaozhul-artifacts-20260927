//! Axum HTTP transport.
//!
//! Endpoints (all bodies JSON, every error body identical in shape):
//!
//! | method | path                          | meaning                          |
//! |--------|-------------------------------|----------------------------------|
//! | GET    | `/healthz`                    | liveness                         |
//! | POST   | `/monitors`                   | create a monitor                 |
//! | GET    | `/monitors`                   | list monitor ids                 |
//! | GET    | `/monitors/{id}`               | status + three-valued verdict    |
//! | POST   | `/monitors/{id}/events`        | append one event or a batch      |
//! | POST   | `/monitors/{id}/end`           | close the trace                  |
//! | POST   | `/monitors/{id}/rotate`        | swap ruleset version (new epoch) |
//! | GET    | `/monitors/{id}/obligations`   | all obligation instances         |
//! | GET    | `/monitors/{id}/rules`         | per-rule roll-up                 |
//! | GET    | `/monitors/{id}/decisions`     | tail of the hash-chained journal |
//! | GET    | `/monitors/{id}/snapshot`      | portable snapshot with digest    |
//! | POST   | `/monitors/{id}/restore`       | restore/verify a snapshot        |
//! | POST   | `/monitors/{id}/verify`        | re-verify evidence independently |

use std::sync::Arc;

use axum::{
    body::Bytes,
    extract::{Path, State},
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};
use uuid::Uuid;

use crate::error::{ErrorBody, ErrorDetail, KernelError};
use crate::lang::{Event, RuleSet};
use crate::monitor::{Limits, Monitor};
use crate::store::Store;

#[derive(Clone)]
pub struct AppState {
    pub store: Arc<Store>,
}

pub fn router(store: Arc<Store>) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/monitors", get(list_monitors).post(create_monitor))
        .route("/monitors/{id}", get(get_monitor))
        .route("/monitors/{id}/events", post(append_events))
        .route("/monitors/{id}/end", post(end_monitor))
        .route("/monitors/{id}/rotate", post(rotate_monitor))
        .route("/monitors/{id}/obligations", get(obligations))
        .route("/monitors/{id}/rules", get(rules))
        .route("/monitors/{id}/decisions", get(decisions))
        .route("/monitors/{id}/snapshot", get(snapshot))
        .route("/monitors/{id}/restore", post(restore))
        .route("/monitors/{id}/verify", post(verify))
        .with_state(AppState { store })
}

async fn healthz() -> Json<Value> {
    Json(json!({"status": "ok"}))
}

// ---------------------------------------------------------------- transport
struct AppError(KernelError, String);

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let AppError(e, run_id) = self;
        let status =
            StatusCode::from_u16(e.http_status()).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
        let body = ErrorBody {
            error: ErrorDetail {
                code: e.code.to_string(),
                category: e.category_name().to_string(),
                message: e.message.clone(),
                run_id,
            },
        };
        (status, Json(body)).into_response()
    }
}

fn err(store: &AppState, run_id: &str, e: KernelError) -> AppError {
    let _ = store;
    AppError(e, run_id.to_string())
}

/// Read (or mint) the correlation id. Every response and every journal entry
/// carry it, so a failing request can be replayed end to end.
fn run_id(headers: &HeaderMap) -> String {
    headers
        .get("x-run-id")
        .and_then(|v| v.to_str().ok())
        .map(str::to_string)
        .filter(|s| !s.trim().is_empty() && s.len() <= 128)
        .unwrap_or_else(|| Uuid::new_v4().to_string())
}

fn ok<T: serde::Serialize>(run_id: &str, value: T) -> Response {
    (
        StatusCode::OK,
        [(
            header::HeaderName::from_static("x-run-id"),
            run_id.to_string(),
        )],
        Json(json!({ "run_id": run_id, "data": value })),
    )
        .into_response()
}

/// Parse a body while enforcing the local size budget.
fn parse<T: for<'de> Deserialize<'de>>(
    bytes: &Bytes,
    max: usize,
) -> std::result::Result<T, KernelError> {
    if bytes.len() > max {
        return Err(KernelError::input(
            "BODY_TOO_LARGE",
            format!("body exceeds {max} bytes"),
        ));
    }
    serde_json::from_slice(bytes)
        .map_err(|e| KernelError::input("MALFORMED_JSON", format!("invalid JSON body: {e}")))
}

// ------------------------------------------------------------------ DTOs
#[derive(Debug, Deserialize)]
struct CreateRequest {
    #[serde(default)]
    monitor_id: Option<String>,
    ruleset: RuleSet,
    #[serde(default)]
    limits: Option<Limits>,
}

/// Events body is either a bare event or `{"events":[...], "steps":[...]}`.
/// A manual `Deserialize` is required because [`Event`] flattens arbitrary
/// attributes: with an untagged enum, `{"events":[...]}` would silently parse
/// as one event carrying an `events` attribute instead of a batch.
#[derive(Debug)]
enum EventsBody {
    One(Event),
    Many {
        events: Vec<Event>,
        steps: Option<Vec<i64>>,
    },
}

impl<'de> Deserialize<'de> for EventsBody {
    fn deserialize<D>(de: D) -> std::result::Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let v = Value::deserialize(de)?;
        match v.get("events") {
            Some(Value::Array(_)) => {
                #[derive(Deserialize)]
                struct Many {
                    events: Vec<Event>,
                    #[serde(default)]
                    steps: Option<Vec<i64>>,
                }
                let Many { events, steps } =
                    serde_json::from_value(v).map_err(serde::de::Error::custom)?;
                Ok(EventsBody::Many { events, steps })
            }
            _ => serde_json::from_value(v)
                .map(EventsBody::One)
                .map_err(serde::de::Error::custom),
        }
    }
}

#[derive(Debug, Deserialize)]
struct DecisionsQuery {
    limit: Option<usize>,
}

#[derive(Debug, Deserialize)]
struct RestoreRequest {
    snapshot: Value,
    #[serde(default)]
    expected_version: Option<String>,
}

// --------------------------------------------------------------- handlers
async fn list_monitors(State(st): State<AppState>) -> Response {
    let ids = st.store.list();
    ok("list", json!({"monitors": ids}))
}

async fn create_monitor(State(st): State<AppState>, headers: HeaderMap, bytes: Bytes) -> Response {
    let rid = run_id(&headers);
    let req: CreateRequest = match parse(&bytes, 1 << 20) {
        Ok(v) => v,
        Err(e) => return err(&st, &rid, e).into_response(),
    };
    let id = req
        .monitor_id
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    let m = match Monitor::new(
        id.clone(),
        rid.clone(),
        req.ruleset,
        req.limits.unwrap_or_default(),
    ) {
        Ok(m) => m,
        Err(e) => return err(&st, &rid, e).into_response(),
    };
    if let Err(e) = st.store.insert(m) {
        return err(&st, &rid, e).into_response();
    }
    let status = match st.store.with(&id, |m| Ok(m.status())) {
        Ok(s) => s,
        Err(e) => return err(&st, &rid, e).into_response(),
    };
    ok(&rid, json!({"monitor_id": id, "status": status}))
}

async fn get_monitor(
    State(st): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    let rid = run_id(&headers);
    match st.store.with(&id, |m| Ok(m.status())) {
        Ok(s) => ok(&rid, s),
        Err(e) => err(&st, &rid, e).into_response(),
    }
}

async fn append_events(
    State(st): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    bytes: Bytes,
) -> Response {
    let rid = run_id(&headers);
    let body: EventsBody = match parse(&bytes, 1 << 20) {
        Ok(v) => v,
        Err(e) => return err(&st, &rid, e).into_response(),
    };
    let (events, steps): (Vec<Event>, Vec<Option<i64>>) = match body {
        EventsBody::One(ev) => (vec![ev], vec![None]),
        EventsBody::Many { events, steps } => {
            if let Some(steps) = steps {
                if steps.len() != events.len() {
                    return err(
                        &st,
                        &rid,
                        KernelError::input(
                            "STEP_LIST_LENGTH",
                            "steps length must equal events length",
                        ),
                    )
                    .into_response();
                }
                (events, steps.into_iter().map(Some).collect())
            } else {
                let n = events.len();
                (events, vec![None; n])
            }
        }
    };

    // Atomic batch: replay against a clone, commit only if every append
    // succeeded. The monitor's own ordering checks reject gaps/regressions.
    let outcome = st.store.with(&id, |m| {
        let mut draft = try_clone(m)?;
        let mut reports = Vec::new();
        for (ev, step) in events.iter().zip(steps.iter()) {
            reports.push(draft.append(ev, *step)?);
        }
        *m = draft;
        Ok(reports)
    });
    match outcome {
        Ok(reports) => ok(&rid, json!({"results": reports})),
        Err(e) => err(&st, &rid, e).into_response(),
    }
}

async fn end_monitor(
    State(st): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    let rid = run_id(&headers);
    match st.store.with(&id, |m| m.end()) {
        Ok(r) => ok(&rid, r),
        Err(e) => err(&st, &rid, e).into_response(),
    }
}

async fn rotate_monitor(
    State(st): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    bytes: Bytes,
) -> Response {
    let rid = run_id(&headers);
    let req: Value = match parse(&bytes, 1 << 20) {
        Ok(v) => v,
        Err(e) => return err(&st, &rid, e).into_response(),
    };
    let ruleset: RuleSet = match serde_json::from_value(req) {
        Ok(r) => r,
        Err(e) => {
            return err(
                &st,
                &rid,
                KernelError::input("MALFORMED_RULESET", format!("invalid ruleset: {e}")),
            )
            .into_response()
        }
    };
    match st.store.with(&id, |m| m.rotate(ruleset)) {
        Ok(r) => ok(&rid, r),
        Err(e) => err(&st, &rid, e).into_response(),
    }
}

async fn obligations(
    State(st): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    let rid = run_id(&headers);
    match st.store.with(&id, |m| Ok(m.obligations.clone())) {
        Ok(o) => ok(&rid, json!({"obligations": o})),
        Err(e) => err(&st, &rid, e).into_response(),
    }
}

async fn rules(State(st): State<AppState>, headers: HeaderMap, Path(id): Path<String>) -> Response {
    let rid = run_id(&headers);
    match st.store.with(&id, |m| Ok(m.rule_outcomes())) {
        Ok(r) => ok(&rid, json!({"rules": r})),
        Err(e) => err(&st, &rid, e).into_response(),
    }
}

async fn decisions(
    State(st): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    axum::extract::Query(q): axum::extract::Query<DecisionsQuery>,
) -> Response {
    let rid = run_id(&headers);
    match st.store.with(&id, |m| {
        let n = m.decisions.len();
        let take = q.limit.unwrap_or(n).min(n);
        Ok(m.decisions[n - take..].to_vec())
    }) {
        Ok(d) => ok(&rid, json!({"decisions": d, "count": d.len()})),
        Err(e) => err(&st, &rid, e).into_response(),
    }
}

async fn snapshot(
    State(st): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    let rid = run_id(&headers);
    match st.store.with(&id, |m| Ok(m.snapshot())) {
        Ok(s) => ok(&rid, s),
        Err(e) => err(&st, &rid, e).into_response(),
    }
}

async fn restore(
    State(st): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    bytes: Bytes,
) -> Response {
    let rid = run_id(&headers);
    let req: RestoreRequest = match parse(&bytes, 8 << 20) {
        Ok(v) => v,
        Err(e) => return err(&st, &rid, e).into_response(),
    };
    let snap_monitor = req
        .snapshot
        .get("monitor_id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if snap_monitor != id {
        return err(
            &st,
            &rid,
            KernelError::snapshot_id_mismatch(&snap_monitor, &id),
        )
        .into_response();
    }
    let restored = match Monitor::restore(req.snapshot, req.expected_version.as_deref()) {
        Ok(m) => m,
        Err(e) => return err(&st, &rid, e).into_response(),
    };
    // Replace atomically: take old value out, insert restored value; on insert
    // failure (duplicate id cannot happen here since we removed it) put old
    // back.
    let old = st.store.take(&id);
    if let Err(e) = st.store.insert(restored) {
        if let Ok(old) = old {
            let _ = st.store.insert(old);
        }
        return err(&st, &rid, e).into_response();
    }
    ok(&rid, json!({"restored": id}))
}

async fn verify(
    State(st): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    let rid = run_id(&headers);
    match st.store.with(&id, |m| {
        let snap = m.snapshot();
        // Independently recompute the snapshot digest and the chain.
        let claimed = snap.get("digest").and_then(|v| v.as_str()).unwrap_or("");
        let recomputed = crate::canonical::canonical_digest(&snap);
        if claimed != recomputed {
            return Err(KernelError::snapshot_digest());
        }
        m.verify_chain()?;
        Ok(json!({
            "verified": true,
            "entries": m.decisions.len(),
            "obligations": m.obligations.len(),
            "digest": claimed,
            "global_verdict": m.global_verdict(),
        }))
    }) {
        Ok(v) => ok(&rid, v),
        Err(e) => err(&st, &rid, e).into_response(),
    }
}

/// Snapshot→Monitor clone used for atomic batch appends. Going through the
/// snapshot representation guarantees a failed batch cannot leave the live
/// monitor half-mutated, and incidentally exercises snapshot round-trip.
fn try_clone(m: &Monitor) -> std::result::Result<Monitor, KernelError> {
    let snap = m.snapshot();
    Monitor::restore(snap, None)
}
