//! Request/response wire model with hand-rolled JSON (no serde in the
//! server), so the shapes here are explicit and string-escaping is local.

use std::collections::BTreeMap;

/// One processing step for the explainability trace.
#[derive(Debug, Clone)]
pub struct Step {
    /// Stage name, e.g. `parse_body`, `load_set`, `oracle_cross_check`.
    pub stage: String,
    /// What happened, with concrete values.
    pub detail: String,
}

/// Top-level envelope returned by every handler.
#[derive(Debug)]
pub struct Envelope {
    pub request_id: String,
    pub version: &'static str,
    pub ok: bool,
    /// Machine-readable error category, present iff `ok == false`.
    pub error_code: Option<String>,
    /// Human-readable failure reason.
    pub error_message: Option<String>,
    /// Successful-result payload fields.
    pub result: BTreeMap<String, Json>,
    /// Ordered explainability steps.
    pub steps: Vec<Step>,
    /// Things that could not be determined with certainty; always rendered
    /// separately even on success.
    pub uncertainties: Vec<String>,
}

/// Minimal JSON value type for our flat/nested result payloads.
#[derive(Debug, Clone)]
pub enum Json {
    Null,
    Bool(bool),
    Int(i64),
    UInt(u64),
    Str(String),
    Array(Vec<Json>),
    Object(Vec<(String, Json)>),
}

impl Json {
    pub fn str(s: impl Into<String>) -> Self {
        Json::Str(s.into())
    }
    pub fn u(n: u64) -> Self {
        Json::UInt(n)
    }
    pub fn arr(items: impl IntoIterator<Item = Json>) -> Self {
        Json::Array(items.into_iter().collect())
    }
    pub fn obj(fields: Vec<(&str, Json)>) -> Self {
        Json::Object(
            fields
                .into_iter()
                .map(|(k, v)| (k.to_string(), v))
                .collect(),
        )
    }
}

fn escape(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write_value(v: &Json, out: &mut String) {
    match v {
        Json::Null => out.push_str("null"),
        Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Json::Int(n) => out.push_str(&n.to_string()),
        Json::UInt(n) => out.push_str(&n.to_string()),
        Json::Str(s) => escape(s, out),
        Json::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(item, out);
            }
            out.push(']');
        }
        Json::Object(fields) => {
            out.push('{');
            for (i, (k, val)) in fields.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                escape(k, out);
                out.push(':');
                write_value(val, out);
            }
            out.push('}');
        }
    }
}

impl Envelope {
    pub fn success(request_id: impl Into<String>) -> Self {
        Self {
            request_id: request_id.into(),
            version: env!("CARGO_PKG_VERSION"),
            ok: true,
            error_code: None,
            error_message: None,
            result: BTreeMap::new(),
            steps: Vec::new(),
            uncertainties: Vec::new(),
        }
    }

    pub fn error(request_id: impl Into<String>, code: &str, message: impl Into<String>) -> Self {
        Self {
            request_id: request_id.into(),
            version: env!("CARGO_PKG_VERSION"),
            ok: false,
            error_code: Some(code.to_string()),
            error_message: Some(message.into()),
            result: BTreeMap::new(),
            steps: Vec::new(),
            uncertainties: Vec::new(),
        }
    }

    pub fn step(mut self, stage: &str, detail: impl Into<String>) -> Self {
        self.steps.push(Step {
            stage: stage.to_string(),
            detail: detail.into(),
        });
        self
    }

    pub fn uncertain(mut self, note: impl Into<String>) -> Self {
        self.uncertainties.push(note.into());
        self
    }

    pub fn put(&mut self, key: &str, value: Json) {
        self.result.insert(key.to_string(), value);
    }

    /// Serialise with result fields in a stable, documented key order
    /// (alphabetical via BTreeMap; steps and metadata fixed).
    pub fn to_json(&self) -> String {
        let mut out = String::new();
        out.push('{');
        out.push_str("\"request_id\":");
        escape(&self.request_id, &mut out);
        out.push_str(",\"version\":");
        escape(self.version, &mut out);
        out.push_str(",\"ok\":");
        out.push_str(if self.ok { "true" } else { "false" });

        if let (Some(code), Some(msg)) = (&self.error_code, &self.error_message) {
            out.push_str(",\"error_code\":");
            escape(code, &mut out);
            out.push_str(",\"error_message\":");
            escape(msg, &mut out);
        }

        out.push_str(",\"result\":{");
        for (i, (k, v)) in self.result.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            escape(k, &mut out);
            out.push(':');
            write_value(v, &mut out);
        }
        out.push('}');

        out.push_str(",\"steps\":[");
        for (i, s) in self.steps.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            out.push_str("{\"stage\":");
            escape(&s.stage, &mut out);
            out.push_str(",\"detail\":");
            escape(&s.detail, &mut out);
            out.push('}');
        }
        out.push(']');

        out.push_str(",\"uncertainties\":[");
        for (i, u) in self.uncertainties.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            escape(u, &mut out);
        }
        out.push(']');

        out.push('}');
        out
    }
}

/// Tiny parser for the request bodies we accept: flat JSON objects whose
/// values are arrays of integers, strings, booleans or integers. Returns
/// owned fields in insertion order.
pub fn parse_flat_json_object(body: &str) -> Result<Vec<(String, Json)>, String> {
    // This is deliberately constrained: use a small recursive-descent
    // parser supporting objects/arrays/strings/numbers/true/false/null.
    let bytes = body.as_bytes();
    let mut p = Parser { bytes, pos: 0 };
    p.ws();
    let value = p.value().map_err(|e| format!("invalid JSON: {e}"))?;
    p.ws();
    if p.pos != bytes.len() {
        return Err("trailing characters after JSON body".to_string());
    }
    match value {
        Json::Object(fields) => Ok(fields),
        _ => Err("request body must be a JSON object".to_string()),
    }
}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn ws(&mut self) {
        while self.pos < self.bytes.len()
            && matches!(self.bytes[self.pos], b' ' | b'\t' | b'\n' | b'\r')
        {
            self.pos += 1;
        }
    }

    fn value(&mut self) -> Result<Json, String> {
        self.ws();
        if self.pos >= self.bytes.len() {
            return Err("unexpected end".to_string());
        }
        match self.bytes[self.pos] {
            b'{' => self.object(),
            b'[' => self.array(),
            b'"' => Ok(Json::Str(self.string()?)),
            b't' | b'f' => self.boolean(),
            b'n' => self.null(),
            b'-' | b'0'..=b'9' => self.number(),
            other => Err(format!("unexpected byte {other:#x}")),
        }
    }

    fn object(&mut self) -> Result<Json, String> {
        self.pos += 1;
        let mut fields = Vec::new();
        self.ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Json::Object(fields));
        }
        loop {
            self.ws();
            let key = self.string()?;
            self.ws();
            self.expect(b':')?;
            let value = self.value()?;
            fields.push((key, value));
            self.ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b'}') => {
                    self.pos += 1;
                    break;
                }
                _ => return Err("expected , or } in object".to_string()),
            }
        }
        Ok(Json::Object(fields))
    }

    fn array(&mut self) -> Result<Json, String> {
        self.pos += 1;
        let mut items = Vec::new();
        self.ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Json::Array(items));
        }
        loop {
            items.push(self.value()?);
            self.ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b']') => {
                    self.pos += 1;
                    break;
                }
                _ => return Err("expected , or ] in array".to_string()),
            }
        }
        Ok(Json::Array(items))
    }

    fn string(&mut self) -> Result<String, String> {
        self.expect(b'"')?;
        let mut s = String::new();
        loop {
            if self.pos >= self.bytes.len() {
                return Err("unterminated string".to_string());
            }
            let c = self.bytes[self.pos];
            self.pos += 1;
            match c {
                b'"' => break,
                b'\\' => {
                    let e = self
                        .bytes
                        .get(self.pos)
                        .ok_or_else(|| "bad escape".to_string())?;
                    self.pos += 1;
                    match e {
                        b'"' => s.push('"'),
                        b'\\' => s.push('\\'),
                        b'/' => s.push('/'),
                        b'n' => s.push('\n'),
                        b't' => s.push('\t'),
                        b'r' => s.push('\r'),
                        b'u' => {
                            if self.pos + 4 > self.bytes.len() {
                                return Err("bad unicode escape".to_string());
                            }
                            let hex = std::str::from_utf8(&self.bytes[self.pos..self.pos + 4])
                                .map_err(|_| "bad unicode escape".to_string())?;
                            let code = u32::from_str_radix(hex, 16)
                                .map_err(|_| "bad unicode escape".to_string())?;
                            self.pos += 4;
                            if let Some(ch) = char::from_u32(code) {
                                s.push(ch);
                            }
                        }
                        _ => return Err("unsupported escape".to_string()),
                    }
                }
                other => {
                    // UTF-8 passthrough: collect until next ASCII control
                    // boundary; simplest correct path is to push raw bytes.
                    if other < 0x20 {
                        return Err("control byte in string".to_string());
                    }
                    // Re-assemble the UTF-8 character by finding its length.
                    let start = self.pos - 1;
                    let len = if other < 0x80 {
                        1
                    } else if other >> 5 == 0b110 {
                        2
                    } else if other >> 4 == 0b1110 {
                        3
                    } else {
                        4
                    };
                    if self.pos - 1 + len > self.bytes.len() {
                        return Err("truncated utf-8".to_string());
                    }
                    let chunk = &self.bytes[start..start + len];
                    self.pos = start + len;
                    s.push_str(std::str::from_utf8(chunk).map_err(|_| "bad utf-8")?);
                }
            }
        }
        Ok(s)
    }

    fn number(&mut self) -> Result<Json, String> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        let mut saw_digit = false;
        while let Some(c) = self.peek() {
            if c.is_ascii_digit() {
                saw_digit = true;
                self.pos += 1;
            } else if c == b'.' || c == b'e' || c == b'E' || c == b'+' || c == b'-' {
                // Only integers are part of the contract; reject fractions.
                return Err("only integer JSON numbers are accepted".to_string());
            } else {
                break;
            }
        }
        if !saw_digit {
            return Err("bad number".to_string());
        }
        let text = std::str::from_utf8(&self.bytes[start..self.pos]).map_err(|_| "bad utf-8")?;
        if text.starts_with('-') {
            text.parse::<i64>()
                .map(Json::Int)
                .map_err(|_| "integer out of range".to_string())
        } else {
            text.parse::<u64>()
                .map(Json::UInt)
                .map_err(|_| "integer out of range".to_string())
        }
    }

    fn boolean(&mut self) -> Result<Json, String> {
        if self.bytes[self.pos..].starts_with(b"true") {
            self.pos += 4;
            Ok(Json::Bool(true))
        } else if self.bytes[self.pos..].starts_with(b"false") {
            self.pos += 5;
            Ok(Json::Bool(false))
        } else {
            Err("bad literal".to_string())
        }
    }

    fn null(&mut self) -> Result<Json, String> {
        if self.bytes[self.pos..].starts_with(b"null") {
            self.pos += 4;
            Ok(Json::Null)
        } else {
            Err("bad literal".to_string())
        }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn expect(&mut self, c: u8) -> Result<(), String> {
        if self.peek() == Some(c) {
            self.pos += 1;
            Ok(())
        } else {
            Err(format!("expected {:#x}", c))
        }
    }
}
