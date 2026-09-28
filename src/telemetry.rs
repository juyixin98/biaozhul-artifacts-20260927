//! 运行身份与日志：
//! - `run_id`：进程级唯一标识，进入每条错误响应与日志，失败可关联到具体运行。
//! - 请求 id：客户端可用 `X-Request-Id` 传入（受限字符集）；否则服务端生成。
//! - 所有失败都按错误类别记录为 warn/error，绝不“异常也 200”。

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

/// 进程运行身份。
#[derive(Debug)]
pub struct RunIdentity {
    pub run_id: String,
    req_counter: AtomicU64,
}

impl RunIdentity {
    pub fn new() -> RunIdentity {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let run_id = format!(
            "run-{:016x}-pid{}",
            nanos as u64 ^ ((std::process::id() as u64) << 17),
            std::process::id()
        );
        RunIdentity {
            run_id,
            req_counter: AtomicU64::new(1),
        }
    }

    /// 生成服务端请求 id：`<run_id>-req<n>`。
    pub fn next_request_id(&self) -> String {
        let n = self.req_counter.fetch_add(1, Ordering::Relaxed);
        format!("{}-req{n}", self.run_id)
    }
}

impl Default for RunIdentity {
    fn default() -> Self {
        Self::new()
    }
}

/// 校验客户端请求 id：1..=64 个可见 ASCII 安全字符，不含空白/引号/反斜杠。
pub fn is_valid_request_id(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && s.bytes()
            .all(|b| b.is_ascii_graphic() && b != b'"' && b != b'\\' && b != b',')
}

pub fn init_tracing(filter: &str) {
    use tracing_subscriber::{fmt, EnvFilter};
    let env_filter = EnvFilter::try_new(filter).unwrap_or_else(|_| EnvFilter::new("info"));
    let _ = fmt()
        .with_env_filter(env_filter)
        .with_level(true)
        .with_target(false)
        .try_init();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_id_rules() {
        assert!(is_valid_request_id("case-hand-001"));
        assert!(!is_valid_request_id(""));
        assert!(!is_valid_request_id("has space"));
        assert!(!is_valid_request_id("bad\"quote"));
        assert!(!is_valid_request_id(&"x".repeat(65)));
        assert!(is_valid_request_id(&"x".repeat(64)));
    }

    #[test]
    fn generated_ids_carry_run_identity_and_monotonic() {
        let r = RunIdentity::new();
        let a = r.next_request_id();
        let b = r.next_request_id();
        assert!(a.starts_with(&r.run_id));
        assert_ne!(a, b);
        assert!(b.ends_with("-req2"));
    }
}
