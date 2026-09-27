//! HTTP 请求/响应数据契约（JSON DTO）。
//!
//! 二进制模式（任意字节，含 0x00）通过 `pattern_base64` 传输；
//! 纯可读场景用 `pattern`（UTF-8 字符串）。二者同时给且不相等报 invalid_input。

use base64::Engine;
use serde::{Deserialize, Serialize};

use crate::error::{FmError, Result};
use crate::persist::Manifest;
use crate::service::SearchResult;

// ---------------- 创建索引 ----------------

#[derive(Debug, Deserialize)]
pub struct CreateIndexReq {
    pub name: String,
    /// 直接给文本内容（UTF-8 JSON 字符串，能表达任意 Unicode 文本的字节）。
    pub text: Option<String>,
    /// 直接给原始字节（base64），用于二进制零字节夹具。
    pub text_base64: Option<String>,
    /// 或从白名单目录导入相对路径。
    pub path: Option<String>,
    #[serde(default)]
    pub rank_block: Option<u32>,
    #[serde(default)]
    pub sample_step: Option<u32>,
}

impl CreateIndexReq {
    /// 校验并取出建索引方式：内联字节 或 文件路径（互斥）。
    pub fn into_source(self) -> Result<IndexSource> {
        let n_inline = self.text.is_some() as u8 + self.text_base64.is_some() as u8;
        let n_path = self.path.is_some() as u8;
        if n_inline + n_path != 1 {
            return Err(FmError::invalid_input(
                "text / text_base64 / path 三者必须且只能提供一个",
            ));
        }
        if let Some(rel) = self.path {
            return Ok(IndexSource::File(rel));
        }
        if let Some(b64) = self.text_base64 {
            let bytes = decode_b64(&b64, "text_base64")?;
            return Ok(IndexSource::Inline(bytes));
        }
        Ok(IndexSource::Inline(self.text.unwrap().into_bytes()))
    }
}

/// 建索引输入来源。
pub enum IndexSource {
    Inline(Vec<u8>),
    File(String),
}

// ---------------- 搜索 ----------------

#[derive(Debug, Default, Deserialize)]
pub struct SearchReq {
    pub pattern: Option<String>,
    pub pattern_base64: Option<String>,
    /// 显式查询空模式（因为 JSON 中省略与空串都可能出现）。
    #[serde(default)]
    pub empty: bool,
    #[serde(default)]
    pub trace: bool,
}

impl SearchReq {
    /// 解析成模式字节。规则：
    /// - `empty=true`：模式必须为空（不能再给 pattern），结果为 Vec::new()；
    /// - 给了 `pattern_base64`：按原始字节；同时给 `pattern` 时必须字节一致；
    /// - 否则用 `pattern`（缺省视为空字符串，等同空模式，与 empty=true 等价）。
    pub fn into_pattern(self) -> Result<Vec<u8>> {
        if self.empty {
            if self.pattern.is_some() || self.pattern_base64.is_some() {
                return Err(FmError::invalid_input(
                    "empty=true 时不能同时提供 pattern/pattern_base64",
                ));
            }
            return Ok(Vec::new());
        }
        match (self.pattern, self.pattern_base64) {
            (Some(s), Some(b64)) => {
                let bytes = decode_b64(&b64, "pattern_base64")?;
                if bytes != s.as_bytes() {
                    return Err(FmError::invalid_input(
                        "pattern 与 pattern_base64 解码后不一致",
                    ));
                }
                Ok(bytes)
            }
            (Some(s), None) => Ok(s.into_bytes()),
            (None, Some(b64)) => decode_b64(&b64, "pattern_base64"),
            // 不带任何字段：视为空模式（GET 查询的自然写法）。
            (None, None) => Ok(Vec::new()),
        }
    }
}

fn decode_b64(b64: &str, field: &str) -> Result<Vec<u8>> {
    base64::engine::general_purpose::STANDARD
        .decode(b64.trim())
        .map_err(|e| FmError::invalid_input(format!("{field} 不是合法 base64: {e}")))
}

/// GET 查询参数（axum Query 提取器自动 urlencoded 反序列化）。
#[derive(Debug, Default, Deserialize)]
pub struct SearchQuery {
    #[serde(default)]
    pub pattern: Option<String>,
    #[serde(default)]
    pub pattern_base64: Option<String>,
    #[serde(default)]
    pub empty: bool,
    #[serde(default)]
    pub trace: bool,
}

impl SearchQuery {
    pub fn into_pattern(self) -> Result<Vec<u8>> {
        SearchReq {
            pattern: self.pattern,
            pattern_base64: self.pattern_base64,
            empty: self.empty,
            trace: false,
        }
        .into_pattern()
    }
}

/// 极简 percent-decode 工具（保留供未来非 axum 场景使用）。
#[allow(dead_code)]
fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                let h = std::str::from_utf8(&bytes[i + 1..i + 3])
                    .ok()
                    .and_then(|x| u8::from_str_radix(x, 16).ok());
                match h {
                    Some(b) => {
                        out.push(b);
                        i += 3;
                    }
                    None => {
                        out.push(bytes[i]);
                        i += 1;
                    }
                }
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

// ---------------- 响应 DTO ----------------

#[derive(Debug, Serialize)]
pub struct ManifestView {
    pub name: String,
    pub encoded_len: u64,
    pub text_len: u64,
    pub rank_block: u32,
    pub sample_step: u32,
    pub sample_count: u64,
    pub created_at: u64,
}

impl ManifestView {
    pub fn new(name: String, m: &Manifest) -> Self {
        ManifestView {
            name,
            encoded_len: m.encoded_len,
            text_len: m.text_len,
            rank_block: m.rank_block,
            sample_step: m.sample_step,
            sample_count: m.sample_count,
            created_at: m.created_at,
        }
    }
}

#[derive(Debug, Serialize)]
pub struct IndexList {
    pub loaded: Vec<String>,
    pub on_disk: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct SearchView {
    pub pattern_len: u64,
    pub pattern_base64: String,
    pub count: u64,
    pub interval: IntervalView,
    pub locations: Vec<u64>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub trace: Vec<crate::fm::SearchStep>,
}

#[derive(Debug, Serialize)]
pub struct IntervalView {
    pub lo: u64,
    pub hi: u64,
    /// 区间为半开 [lo, hi)，空区间时 lo == hi。
    pub half_open: bool,
}

impl SearchView {
    pub fn from_result(pattern: &[u8], r: SearchResult) -> Self {
        SearchView {
            pattern_len: pattern.len() as u64,
            pattern_base64: base64::engine::general_purpose::STANDARD.encode(pattern),
            count: r.count,
            interval: IntervalView {
                lo: r.interval_lo,
                hi: r.interval_hi,
                half_open: true,
            },
            locations: r.locations,
            trace: r.steps,
        }
    }
}

#[derive(Debug, Serialize)]
pub struct CountView {
    pub pattern_len: u64,
    pub count: u64,
}

#[derive(Debug, Serialize)]
pub struct HealthView {
    pub status: &'static str,
    pub loaded: Vec<String>,
    pub on_disk: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct DeletedView {
    pub deleted: String,
}
