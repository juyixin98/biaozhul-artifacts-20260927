//! Axum HTTP validation/service interface.
//!
//! Endpoints (JSON, binary fields are standard base64):
//!
//! | method | path                              | purpose |
//! |--------|-----------------------------------|---------|
//! | GET    | `/healthz`                        | liveness + store stats |
//! | POST   | `/v1/streams`                     | create a stream |
//! | POST   | `/v1/streams/:id/blocks`          | append an externally-built block |
//! | POST   | `/v1/streams/:id/encode`          | encode+append the next block |
//! | GET    | `/v1/streams/:id/decode`          | decode the whole stream from disk |
//! | POST   | `/v1/encode-independent`          | stateless one-shot encode |
//! | POST   | `/v1/decode`                      | stateless one-shot block decode |
//!
//! Every request gets a `run id` (client-supplied `X-Run-Id`, else generated),
//! echoed back in the response header and body, and logged with the outcome.

pub mod handlers;
pub mod routes;

pub use handlers::AppState;
pub use routes::router;

/// Max accepted request body (generous: 1 MiB input base64-encodes to ~1.4 MiB).
pub const MAX_BODY: usize = 2 * 1024 * 1024;
