//! Per-request run id carried through request extensions.
//!
//! The middleware inserts [`RunId`]; handlers read it via
//! [`axum::extract::Extension`]. It is also echoed back as the `x-run-id`
//! response header.

use axum::http::HeaderValue;

/// Correlation identifier for one HTTP request.
#[derive(Clone, Debug)]
pub struct RunId(pub String);

impl RunId {
    /// Borrow the string id.
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Header value form.
    pub fn header_value(&self) -> HeaderValue {
        HeaderValue::from_str(&self.0).unwrap_or_else(|_| HeaderValue::from_static("r-invalid"))
    }
}

/// Generate an id: `r-<unix millis hex>-<16 hex entropy chars>`.
pub fn generate() -> String {
    let millis = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    // Cheap deterministic-per-process-ish entropy without a dependency.
    let now = std::time::SystemTime::now();
    let nanos = now
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.subsec_nanos() as u64)
        .unwrap_or(0);
    // A stack address supplies cheap ASLR-ish per-process entropy; nanos
    // decorrelate ids generated within the same process.
    let stack_guard = 0u8;
    let seed = nanos ^ (&stack_guard as *const u8 as u64) ^ millis as u64;
    let mut x = seed | 1;
    let mut bytes = [0u8; 8];
    for b in bytes.iter_mut() {
        // xorshift64
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        *b = (x >> 24) as u8;
    }
    format!("r-{millis:x}-{}", hex_lower(&bytes))
}

fn hex_lower(bytes: &[u8]) -> String {
    const H: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(H[(b >> 4) as usize] as char);
        s.push(H[(b & 0xF) as usize] as char);
    }
    s
}
