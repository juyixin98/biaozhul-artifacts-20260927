//! 错误 → HTTP 响应映射。失败类别保持稳定，外部测试可断言 `errors[0].code`。

use axum::extract::rejection::JsonRejection;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;

use crate::model::{Envelope, ErrorDetail};
/// 接口层错误。处理器应通过 [`ApiError::into_response_for`] 转换，以便携带请求 id。
#[derive(Debug)]
pub struct ApiError {
    pub status: StatusCode,
    pub code: &'static str,
    pub message: String,
    pub location: Option<String>,
}

impl ApiError {
    pub fn bad_request(msg: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::BAD_REQUEST,
            code: "bad_request",
            message: msg.into(),
            location: None,
        }
    }
    pub fn not_found(name: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::NOT_FOUND,
            code: "set_not_found",
            message: "set not found".into(),
            location: Some(name.into()),
        }
    }
    pub fn conflict(msg: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::CONFLICT,
            code: "already_exists",
            message: msg.into(),
            location: None,
        }
    }
    pub fn invalid_name(name: &str) -> Self {
        ApiError {
            status: StatusCode::BAD_REQUEST,
            code: "invalid_name",
            message: format!("name must be 1-64 chars of [A-Za-z0-9_-], got {name:?}"),
            location: None,
        }
    }

    /// 带请求身份构造错误响应（失败原因单列在 `errors` 中）。
    pub fn into_response_for(self, request_id: &str) -> Response {
        let body: Envelope<()> = Envelope {
            request_id: request_id.to_string(),
            format_version: format!("{:#010x}", rb_format::FORMAT_VERSION),
            result: None,
            errors: vec![ErrorDetail {
                code: self.code.to_string(),
                message: self.message,
                location: self.location,
            }],
            notes: Vec::new(),
            steps: Vec::new(),
        };
        (self.status, Json(body)).into_response()
    }
}

/// 存储层错误映射。
impl From<rb_persist::StoreError> for ApiError {
    fn from(e: rb_persist::StoreError) -> Self {
        use rb_persist::StoreError::*;
        match e {
            InvalidName(n) => ApiError::invalid_name(&n),
            NotFound(n) => ApiError::not_found(n),
            AlreadyExists(n) => ApiError::conflict(format!("set already exists: {n}")),
            Io(io) => ApiError {
                status: StatusCode::INTERNAL_SERVER_ERROR,
                code: "io_error",
                message: io.to_string(),
                location: None,
            },
            // 数据文件损坏：422 + 细分类别（不是 500，调用方可据此分类断言）。
            Codec(c) => codec_error(c),
        }
    }
}

/// 编解码错误 → 稳定 code 字符串（与 rb_format::CodecError 变体一一对应）。
fn codec_error(c: rb_format::CodecError) -> ApiError {
    use rb_format::CodecError::*;
    let (code, message, location) = match c {
        Truncated { need, have } => (
            "corrupt_truncated",
            format!("file truncated: need {need} bytes, have {have}"),
            None,
        ),
        BadMagic(m) => ("corrupt_bad_magic", format!("bad magic: {m:02X?}"), None),
        UnsupportedVersion(v) => (
            "corrupt_unsupported_version",
            format!("unsupported version {v:#010x}"),
            None,
        ),
        HeaderChecksumMismatch { stored, computed } => (
            "corrupt_header_checksum",
            format!("header CRC32C mismatch: stored {stored:#x}, computed {computed:#x}"),
            Some("header[12..16]".into()),
        ),
        BodyChecksumMismatch { stored, computed } => (
            "corrupt_body_checksum",
            format!("body CRC32C mismatch: stored {stored:#x}, computed {computed:#x}"),
            Some("body trailer".into()),
        ),
        UnknownContainerTag(t) => (
            "corrupt_unknown_container_tag",
            format!("unknown container tag {t}"),
            None,
        ),
        KeysNotSortedUnique { previous, current } => (
            "corrupt_keys_not_sorted",
            format!("keys not strictly ascending: {previous:?} -> {current}"),
            Some(format!("key={current}")),
        ),
        BadOffset { key, offset } => (
            "corrupt_bad_offset",
            format!("bad payload offset {offset}"),
            Some(format!("key={key}")),
        ),
        BadPayloadLength {
            key,
            declared,
            allowed,
        } => (
            "corrupt_bad_payload_length",
            format!("payload length {declared}, allowed {allowed}"),
            Some(format!("key={key}")),
        ),
        ArrayNotSortedUnique { key, index } => (
            "corrupt_array_not_sorted",
            format!("array element {index} violates sorted-unique order"),
            Some(format!("key={key}")),
        ),
        ArrayExceedsThreshold { key, cardinality } => (
            "corrupt_threshold_violation",
            format!("container cardinality {cardinality} violates the 4096 threshold for its type"),
            Some(format!("key={key}")),
        ),
        CardinalityMismatch {
            key,
            declared,
            actual,
        } => (
            "corrupt_cardinality_mismatch",
            format!("declared cardinality {declared}, actual {actual}"),
            Some(format!("key={key}")),
        ),
        TrailingBitsSet { key } => (
            "corrupt_trailing_bits",
            "bits present beyond declared cardinality".into(),
            Some(format!("key={key}")),
        ),
        Io(m) => ("io_error", m, None),
    };
    ApiError {
        status: StatusCode::UNPROCESSABLE_ENTITY,
        code,
        message,
        location,
    }
}

/// JSON 体解析失败 → 400。
impl From<JsonRejection> for ApiError {
    fn from(r: JsonRejection) -> Self {
        ApiError {
            status: StatusCode::BAD_REQUEST,
            code: "invalid_json",
            message: r.body_text(),
            location: None,
        }
    }
}

/// Axum 的 `?` 错误出口：此处尚不知道请求 id，用占位值，
/// 请求 id 中间件会把响应体里的 `request_id` 回填为真实身份。
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        self.into_response_for("unknown")
    }
}
