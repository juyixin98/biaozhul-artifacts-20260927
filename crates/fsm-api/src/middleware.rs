//! Request-identity middleware.
//!
//! Every request is associated with an ID: honor an inbound
//! `x-request-id` if supplied, otherwise synthesize one locally (no external
//! service). The ID is attached to tracing spans, returned in the response
//! header `x-request-id`, and echoed inside JSON bodies so a result can
//! always be correlated with its log lines.

use std::time::Instant;

use axum::extract::Request;
use axum::http::HeaderValue;
use axum::middleware::Next;
use axum::response::Response;
use tracing::Instrument;

#[derive(Clone, Debug)]
pub struct RequestId(pub String);

pub(crate) fn synthesize_id() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    // local, dependency-free pseudo-id: time + an address-derived salt
    let salt = &nanos as *const u128 as usize;
    format!("req-{nanos:x}-{salt:x}")
}

pub async fn layer(mut req: Request, next: Next) -> Response {
    let id = req
        .headers()
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
        .unwrap_or_else(synthesize_id);

    req.extensions_mut().insert(RequestId(id.clone()));
    let span = tracing::info_span!(
        "http",
        request_id = %id,
        method = %req.method(),
        path = %req.uri().path()
    );

    let t0 = Instant::now();
    let fut = next.run(req).instrument(span);
    let mut resp = fut.await;
    resp.headers_mut()
        .insert("x-request-id", HeaderValue::from_str(&id).unwrap());
    let ms = t0.elapsed().as_millis();
    tracing::info!(
        request_id = %id,
        status = %resp.status().as_u16(),
        elapsed_ms = ms,
        "request complete"
    );
    resp
}
