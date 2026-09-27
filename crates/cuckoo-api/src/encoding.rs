//! 键编码：API 接受 UTF-8（默认）或 base64url 键。
//! 测试与日志统一记录实际使用的编码与长度，便于关联输入。

use base64url_support::{decode_b64url, DecodeError};

mod base64url_support {
    // 复用与凭证层相同的无填充 base64url 字母表（独立小实现，避免跨层暴露内部函数）。
    #[derive(Debug)]
    pub struct DecodeError(pub String);

    fn val(c: u8) -> Result<u8, DecodeError> {
        match c {
            b'A'..=b'Z' => Ok(c - b'A'),
            b'a'..=b'z' => Ok(c - b'a' + 26),
            b'0'..=b'9' => Ok(c - b'0' + 52),
            b'-' => Ok(62),
            b'_' => Ok(63),
            other => Err(DecodeError(format!("非法 base64url 字符: {other:#x}"))),
        }
    }

    pub fn decode_b64url(s: &str) -> Result<Vec<u8>, DecodeError> {
        let bytes = s.as_bytes();
        let mut out = Vec::with_capacity(bytes.len() * 3 / 4);
        let mut i = 0;
        while i < bytes.len() {
            let remain = bytes.len() - i;
            if remain < 2 {
                return Err(DecodeError("存在孤立字符".into()));
            }
            let c0 = val(bytes[i])? as u32;
            let c1 = val(bytes[i + 1])? as u32;
            out.push(((c0 << 2) | (c1 >> 4)) as u8);
            if remain >= 3 {
                let c2 = val(bytes[i + 2])? as u32;
                out.push((((c1 & 15) << 4) | (c2 >> 2)) as u8);
                if remain >= 4 {
                    let c3 = val(bytes[i + 3])? as u32;
                    out.push((((c2 & 3) << 6) | c3) as u8);
                }
            }
            i += 4;
        }
        Ok(out)
    }
}

#[derive(Debug, Clone, Copy)]
pub enum KeyEncoding {
    Utf8,
    Base64Url,
}

impl KeyEncoding {
    pub fn parse(name: Option<&str>) -> Result<Self, String> {
        match name.unwrap_or("utf8") {
            "utf8" | "utf-8" | "" => Ok(Self::Utf8),
            "base64url" | "b64url" => Ok(Self::Base64Url),
            other => Err(format!(
                "未知 key_encoding={other:?}，支持 utf8 或 base64url"
            )),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Utf8 => "utf8",
            Self::Base64Url => "base64url",
        }
    }
}

pub fn decode_key(raw: &str, enc: KeyEncoding) -> Result<Vec<u8>, String> {
    if raw.is_empty() {
        return Err("key 不能为空".to_string());
    }
    match enc {
        KeyEncoding::Utf8 => Ok(raw.as_bytes().to_vec()),
        KeyEncoding::Base64Url => {
            decode_b64url(raw).map_err(|DecodeError(e)| format!("base64url 键解码失败: {e}"))
        }
    }
}

/// 日志/错误中安全展示键：UTF-8 键直接截断展示，base64url 键展示前 16 字符 + 长度。
pub fn preview(raw: &str, enc: KeyEncoding) -> String {
    const MAX: usize = 48;
    match enc {
        KeyEncoding::Utf8 if raw.chars().count() <= MAX => raw.to_string(),
        KeyEncoding::Utf8 => {
            let head: String = raw.chars().take(MAX).collect();
            format!("{head}…(+{} chars)", raw.chars().count() - MAX)
        }
        KeyEncoding::Base64Url if raw.len() <= MAX => raw.to_string(),
        KeyEncoding::Base64Url => format!("{}…(len={})", &raw[..16.min(raw.len())], raw.len()),
    }
}
