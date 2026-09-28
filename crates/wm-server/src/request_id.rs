//! Request identity correlation.
//!
//! Each request carries an id from the `X-Request-Id` header (validated to a
//! short ASCII token) or gets a generated `wm-<...>` id. The id is returned
//! in the response header, placed on a tracing span and embedded in every
//! JSON body, so logs, requests and answers can be correlated.

use axum::extract::Request;
use axum::http::HeaderValue;
use axum::middleware::Next;
use axum::response::Response;

/// Per-request extension holding the resolved id.
#[derive(Debug, Clone)]
pub struct RequestId(pub String);

const HEADER: &str = "x-request-id";
const MAX_LEN: usize = 128;

/// Axum middleware resolving and propagating the request id.
pub async fn layer(mut req: Request, next: Next) -> Response {
    let id = req
        .headers()
        .get(HEADER)
        .and_then(|v| v.to_str().ok())
        .filter(|s| {
            !s.is_empty()
                && s.len() <= MAX_LEN
                && s.chars()
                    .all(|c| c.is_ascii_graphic() || c == ' ' || c == '-')
        })
        .map(str::to_string)
        .unwrap_or_else(generate);

    req.extensions_mut().insert(RequestId(id.clone()));

    let span = tracing::info_span!("http_request", request_id = %id, method = %req.method(), uri = %req.uri());
    let mut resp = {
        let _enter = span.enter();
        next.run(req).await
    };
    if let Ok(value) = HeaderValue::from_str(&id) {
        resp.headers_mut().insert(HEADER, value);
    }
    resp
}

impl RequestId {
    /// The resolved id string.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Generate a fresh id outside the middleware (fallback only, e.g.
    /// fallback routes).
    #[must_use]
    pub fn generated() -> String {
        generate()
    }
}

/// Dependency-free id: nanosecond timestamp + process entropy.
fn generate() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let mix = (now as u64)
        .wrapping_mul(0x9e37_79b9_7f4a_7c15)
        .wrapping_add((std::process::id() as u64) << 32)
        .rotate_left(17);
    format!("wm-{mix:016x}")
}
