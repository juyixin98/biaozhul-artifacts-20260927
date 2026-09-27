//! Hand-written lexer. Produces [`Token`]s with byte spans or a [`LexError`].
use crate::span::{loc_at, Span};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Token {
    pub kind: TokenKind,
    pub span: Span,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TokenKind {
    Int(i64),
    Ident(String),
    // keywords
    Input,
    Const,
    Array,
    If,
    Else,
    While,
    Assert,
    Skip,
    /// The single magnitude 2^63 (9223372036854775808), only legal directly
    /// after a unary `-` so that `-9223372036854775808` (i64::MIN) parses.
    MinMag,
    // punctuation
    LBrace,
    RBrace,
    LBracket,
    RBracket,
    LParen,
    RParen,
    Colon,
    Semicolon,
    Comma,
    Assign,
    // operators
    Plus,
    Minus,
    Star,
    Slash,
    Percent,
    Lt,
    Le,
    Gt,
    Ge,
    EqEq,
    NotEq,
    Bang,
    AmpAmp,
    PipePipe,
    Eof,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LexError {
    pub message: String,
    pub span: Span,
}

impl LexError {
    fn at(message: impl Into<String>, source: &str, start: usize, end: usize) -> Self {
        Self {
            message: message.into(),
            span: Span::new(loc_at(source, start), loc_at(source, end)),
        }
    }
}

struct Lexer<'a> {
    src: &'a str,
    bytes: &'a [u8],
    pos: usize,
}

pub fn lex(source: &str) -> Result<Vec<Token>, LexError> {
    let mut lx = Lexer {
        src: source,
        bytes: source.as_bytes(),
        pos: 0,
    };
    let mut tokens = Vec::new();
    loop {
        lx.skip_trivia();
        let start = lx.pos;
        if lx.pos >= lx.bytes.len() {
            tokens.push(Token {
                kind: TokenKind::Eof,
                span: Span::point(loc_at(source, start)),
            });
            return Ok(tokens);
        }
        let b = lx.bytes[lx.pos];
        let kind = if b.is_ascii_digit() {
            lx.lex_number(start)?
        } else if b.is_ascii_alphabetic() || b == b'_' {
            lx.lex_ident()
        } else {
            lx.lex_punct(start)?
        };
        let end = lx.pos;
        tokens.push(Token {
            kind,
            span: Span::new(loc_at(source, start), loc_at(source, end)),
        });
    }
}

impl<'a> Lexer<'a> {
    fn skip_trivia(&mut self) {
        loop {
            while self.pos < self.bytes.len()
                && matches!(self.bytes[self.pos], b' ' | b'\t' | b'\r' | b'\n')
            {
                self.pos += 1;
            }
            // `//` line comment or `/* ... */` block comment (no nesting).
            if self.pos + 1 < self.bytes.len() && &self.bytes[self.pos..self.pos + 2] == b"//" {
                self.pos += 2;
                while self.pos < self.bytes.len() && self.bytes[self.pos] != b'\n' {
                    self.pos += 1;
                }
            } else if self.pos + 1 < self.bytes.len()
                && &self.bytes[self.pos..self.pos + 2] == b"/*"
            {
                self.pos += 2;
                let mut depth = 1usize;
                while self.pos < self.bytes.len() && depth > 0 {
                    if self.pos + 1 < self.bytes.len()
                        && &self.bytes[self.pos..self.pos + 2] == b"/*"
                    {
                        depth += 1;
                        self.pos += 2;
                    } else if self.pos + 1 < self.bytes.len()
                        && &self.bytes[self.pos..self.pos + 2] == b"*/"
                    {
                        depth -= 1;
                        self.pos += 2;
                    } else {
                        self.pos += 1;
                    }
                }
                if depth > 0 {
                    // Unterminated comment: treat as consumed until EOF; the
                    // program then parses as an empty trailing body.
                }
            } else {
                break;
            }
        }
    }

    fn lex_number(&mut self, start: usize) -> Result<TokenKind, LexError> {
        // Parse digits in u64 so we can recognise the one magnitude token
        // 2^63; anything above 2^63 cannot be part of an i64 literal.
        let mut value: u64 = 0;
        let mut saw_digit = false;
        while self.pos < self.bytes.len() && self.bytes[self.pos].is_ascii_digit() {
            saw_digit = true;
            let d = (self.bytes[self.pos] - b'0') as u64;
            value = match value.checked_mul(10).and_then(|v| v.checked_add(d)) {
                Some(v) => v,
                None => {
                    return Err(LexError::at(
                        "integer literal overflows bounded i64 domain [-9223372036854775808, 9223372036854775807]",
                        self.src,
                        start,
                        self.bytes.len().min(self.scan_digits_end(start)),
                    ))
                }
            };
            self.pos += 1;
        }
        debug_assert!(saw_digit);
        const MAG_MIN: u64 = 1u64 << 63;
        if value > MAG_MIN {
            return Err(LexError::at(
                "integer literal overflows bounded i64 domain [-9223372036854775808, 9223372036854775807]",
                self.src,
                start,
                self.pos,
            ));
        }
        if value == MAG_MIN {
            Ok(TokenKind::MinMag)
        } else {
            Ok(TokenKind::Int(value as i64))
        }
    }

    fn scan_digits_end(&self, start: usize) -> usize {
        let mut e = start;
        while e < self.bytes.len() && self.bytes[e].is_ascii_digit() {
            e += 1;
        }
        e
    }

    fn lex_ident(&mut self) -> TokenKind {
        let start = self.pos;
        while self.pos < self.bytes.len()
            && (self.bytes[self.pos].is_ascii_alphanumeric() || self.bytes[self.pos] == b'_')
        {
            self.pos += 1;
        }
        let text = &self.src[start..self.pos];
        match text {
            "input" => TokenKind::Input,
            "const" => TokenKind::Const,
            "array" => TokenKind::Array,
            "if" => TokenKind::If,
            "else" => TokenKind::Else,
            "while" => TokenKind::While,
            "assert" => TokenKind::Assert,
            "skip" => TokenKind::Skip,
            _ => TokenKind::Ident(text.to_string()),
        }
    }

    fn two(&mut self, pair: &[u8; 2], yes: TokenKind, no: TokenKind) -> TokenKind {
        if self.pos + 1 < self.bytes.len() && self.bytes[self.pos + 1] == pair[1] {
            self.pos += 2;
            yes
        } else {
            self.pos += 1;
            no
        }
    }

    fn lex_punct(&mut self, start: usize) -> Result<TokenKind, LexError> {
        let b = self.bytes[self.pos];
        let kind = match b {
            b'{' => {
                self.pos += 1;
                TokenKind::LBrace
            }
            b'}' => {
                self.pos += 1;
                TokenKind::RBrace
            }
            b'[' => {
                self.pos += 1;
                TokenKind::LBracket
            }
            b']' => {
                self.pos += 1;
                TokenKind::RBracket
            }
            b'(' => {
                self.pos += 1;
                TokenKind::LParen
            }
            b')' => {
                self.pos += 1;
                TokenKind::RParen
            }
            b':' => {
                self.pos += 1;
                TokenKind::Colon
            }
            b';' => {
                self.pos += 1;
                TokenKind::Semicolon
            }
            b',' => {
                self.pos += 1;
                TokenKind::Comma
            }
            b'+' => {
                self.pos += 1;
                TokenKind::Plus
            }
            b'-' => {
                self.pos += 1;
                TokenKind::Minus
            }
            b'*' => {
                self.pos += 1;
                TokenKind::Star
            }
            b'/' => {
                self.pos += 1;
                TokenKind::Slash
            }
            b'%' => {
                self.pos += 1;
                TokenKind::Percent
            }
            b'!' => self.two(b"!=", TokenKind::NotEq, TokenKind::Bang),
            b'=' => self.two(b"==", TokenKind::EqEq, TokenKind::Assign),
            b'<' => self.two(b"<=", TokenKind::Le, TokenKind::Lt),
            b'>' => self.two(b">=", TokenKind::Ge, TokenKind::Gt),
            b'&' => {
                if self.pos + 1 < self.bytes.len() && self.bytes[self.pos + 1] == b'&' {
                    self.pos += 2;
                    TokenKind::AmpAmp
                } else {
                    return Err(LexError::at(
                        "unexpected '&'; logical and is written '&&'",
                        self.src,
                        self.pos,
                        self.pos + 1,
                    ));
                }
            }
            b'|' => {
                if self.pos + 1 < self.bytes.len() && self.bytes[self.pos + 1] == b'|' {
                    self.pos += 2;
                    TokenKind::PipePipe
                } else {
                    return Err(LexError::at(
                        "unexpected '|'; logical or is written '||'",
                        self.src,
                        self.pos,
                        self.pos + 1,
                    ));
                }
            }
            other => {
                return Err(LexError::at(
                    format!("unexpected character {:?}", other as char),
                    self.src,
                    start,
                    start + 1,
                ))
            }
        };
        Ok(kind)
    }
}
