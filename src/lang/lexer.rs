//! Lexer for the Boolean surface language.
//!
//! Produces a fully tokenized stream (including a terminal [`Tok::Eof`]) so
//! the parser never has to touch raw bytes. Every token carries a byte
//! [`Span`], which lets parse errors point at the exact rejected input.

/// Half-open byte range `[start, end)` into the source string.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Span {
    pub start: usize,
    pub end: usize,
}

/// A lexed token.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Token {
    pub kind: Tok,
    pub span: Span,
}

/// Token kinds.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tok {
    Ident,
    KwTrue,
    KwFalse,
    LParen,
    RParen,
    Not,
    And,
    Or,
    Xor,
    Implies,
    Equiv,
    Eof,
}

/// Lexer failure with the offending byte position.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LexError {
    pub pos: usize,
    pub found: Option<char>,
    pub message: String,
}

impl std::fmt::Display for LexError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self.found {
            Some(c) => write!(
                f,
                "lex error at byte {} near {:?}: {}",
                self.pos, c, self.message
            ),
            None => write!(f, "lex error at byte {}: {}", self.pos, self.message),
        }
    }
}
impl std::error::Error for LexError {}

/// Turn source text into a token vector ending with [`Tok::Eof`].
pub fn lex(src: &str) -> Result<Vec<Token>, LexError> {
    let bytes = src.as_bytes();
    let mut i = 0;
    let mut out = Vec::new();

    let is_ident_start = |c: u8| c.is_ascii_alphabetic() || c == b'_';
    let is_ident_cont = |c: u8| c.is_ascii_alphanumeric() || c == b'_';

    while i < bytes.len() {
        let start = i;
        let c = bytes[i];
        match c {
            b' ' | b'\t' | b'\n' | b'\r' => i += 1,
            b'(' => {
                out.push(Token {
                    kind: Tok::LParen,
                    span: Span { start, end: i + 1 },
                });
                i += 1;
            }
            b')' => {
                out.push(Token {
                    kind: Tok::RParen,
                    span: Span { start, end: i + 1 },
                });
                i += 1;
            }
            b'!' => {
                out.push(Token {
                    kind: Tok::Not,
                    span: Span { start, end: i + 1 },
                });
                i += 1;
            }
            b'&' => {
                if bytes.get(i + 1) == Some(&b'&') {
                    out.push(Token {
                        kind: Tok::And,
                        span: Span { start, end: i + 2 },
                    });
                    i += 2;
                } else {
                    return Err(LexError {
                        pos: i,
                        found: Some('&'),
                        message: "expected `&&`".into(),
                    });
                }
            }
            b'|' => {
                if bytes.get(i + 1) == Some(&b'|') {
                    out.push(Token {
                        kind: Tok::Or,
                        span: Span { start, end: i + 2 },
                    });
                    i += 2;
                } else {
                    return Err(LexError {
                        pos: i,
                        found: Some('|'),
                        message: "expected `||`".into(),
                    });
                }
            }
            b'^' => {
                out.push(Token {
                    kind: Tok::Xor,
                    span: Span { start, end: i + 1 },
                });
                i += 1;
            }
            b'=' => {
                if bytes.get(i + 1) == Some(&b'-') && bytes.get(i + 2) == Some(&b'>') {
                    out.push(Token {
                        kind: Tok::Equiv,
                        span: Span { start, end: i + 3 },
                    });
                    i += 3;
                } else {
                    return Err(LexError {
                        pos: i,
                        found: Some('='),
                        message: "did you mean `<->` (equivalence)?".into(),
                    });
                }
            }
            b'<' => {
                if bytes.get(i + 1) == Some(&b'-') && bytes.get(i + 2) == Some(&b'>') {
                    out.push(Token {
                        kind: Tok::Equiv,
                        span: Span { start, end: i + 3 },
                    });
                    i += 3;
                } else {
                    return Err(LexError {
                        pos: i,
                        found: Some('<'),
                        message: "expected `<->`".into(),
                    });
                }
            }
            b'-' => {
                if bytes.get(i + 1) == Some(&b'>') {
                    out.push(Token {
                        kind: Tok::Implies,
                        span: Span { start, end: i + 2 },
                    });
                    i += 2;
                } else {
                    return Err(LexError {
                        pos: i,
                        found: Some('-'),
                        message: "expected `->`".into(),
                    });
                }
            }
            _ if is_ident_start(c) => {
                i += 1;
                while i < bytes.len() && is_ident_cont(bytes[i]) {
                    i += 1;
                }
                let text = &src[start..i];
                let kind = match text {
                    "true" => Tok::KwTrue,
                    "false" => Tok::KwFalse,
                    _ => Tok::Ident,
                };
                out.push(Token {
                    kind,
                    span: Span { start, end: i },
                });
            }
            _ => {
                return Err(LexError {
                    pos: i,
                    found: src[i..].chars().next(),
                    message: "unexpected character".into(),
                });
            }
        }
    }

    out.push(Token {
        kind: Tok::Eof,
        span: Span { start: i, end: i },
    });
    Ok(out)
}
