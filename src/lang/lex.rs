//! Hand-written lexer for the input language.

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Token {
    pub kind: TokKind,
    pub span: (u32, u32), // (col_start, col_end), 1-based columns
    pub line: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TokKind {
    Ident(String),
    Int(String, Option<String>), // decimal digits (without 0x), optional width suffix
    HexInt(String, Option<String>), // hex digits (without 0x), optional width suffix
    // keywords
    Param,
    Let,
    If,
    Else,
    While,
    Assert,
    Assume,
    True,
    False,
    // punctuation
    Semi,
    Comma,
    Colon,
    LBrace,
    RBrace,
    LParen,
    RParen,
    // operators
    Plus,
    Minus,
    Star,
    Slash,
    Percent,
    Amp,
    Pipe,
    Caret,
    Shl,
    Shr,
    EqEq,
    NotEq,
    Lt,
    Le,
    Gt,
    Ge,
    Assign,
    AmpAmp,
    PipePipe,
    Bang,
    Tilde,
    Str(String),
    Eof,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LexError {
    pub line: u32,
    pub col: u32,
    pub msg: String,
}

impl std::fmt::Display for LexError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "line {} col {}: {}", self.line, self.col, self.msg)
    }
}

pub fn lex(src: &str) -> Result<Vec<Token>, LexError> {
    let chars: Vec<char> = src.chars().collect();
    let n = chars.len();
    let mut i = 0usize;
    let mut line = 1u32;
    let mut col = 1u32;
    let mut toks = Vec::new();

    macro_rules! mk {
        ($kind:expr, $c0:expr, $c1:expr) => {{
            toks.push(Token {
                kind: $kind,
                span: ($c0, $c1),
                line,
            });
        }};
    }

    while i < n {
        let c = chars[i];
        let c0 = col;
        match c {
            ' ' | '\t' | '\r' => {
                i += 1;
                col += 1;
            }
            '\n' => {
                i += 1;
                line += 1;
                col = 1;
            }
            '/' if i + 1 < n && chars[i + 1] == '/' => {
                i += 2;
                col += 2;
                while i < n && chars[i] != '\n' {
                    i += 1;
                    col += 1;
                }
            }
            '/' if i + 1 < n && chars[i + 1] == '*' => {
                i += 2;
                col += 2;
                while i + 1 < n && !(chars[i] == '*' && chars[i + 1] == '/') {
                    if chars[i] == '\n' {
                        line += 1;
                        col = 1;
                    } else {
                        col += 1;
                    }
                    i += 1;
                }
                if i + 1 >= n {
                    return Err(LexError {
                        line,
                        col,
                        msg: "unterminated block comment".into(),
                    });
                }
                i += 2;
                col += 2;
            }
            ';' => {
                mk!(TokKind::Semi, c0, col);
                i += 1;
                col += 1;
            }
            '/' => {
                mk!(TokKind::Slash, c0, col);
                i += 1;
                col += 1;
            }
            ',' => {
                mk!(TokKind::Comma, c0, col);
                i += 1;
                col += 1;
            }
            ':' => {
                mk!(TokKind::Colon, c0, col);
                i += 1;
                col += 1;
            }
            '{' => {
                mk!(TokKind::LBrace, c0, col);
                i += 1;
                col += 1;
            }
            '}' => {
                mk!(TokKind::RBrace, c0, col);
                i += 1;
                col += 1;
            }
            '(' => {
                mk!(TokKind::LParen, c0, col);
                i += 1;
                col += 1;
            }
            ')' => {
                mk!(TokKind::RParen, c0, col);
                i += 1;
                col += 1;
            }
            '+' => {
                mk!(TokKind::Plus, c0, col);
                i += 1;
                col += 1;
            }
            '-' => {
                mk!(TokKind::Minus, c0, col);
                i += 1;
                col += 1;
            }
            '*' => {
                mk!(TokKind::Star, c0, col);
                i += 1;
                col += 1;
            }
            '%' => {
                mk!(TokKind::Percent, c0, col);
                i += 1;
                col += 1;
            }
            '&' if i + 1 < n && chars[i + 1] == '&' => {
                mk!(TokKind::AmpAmp, c0, col + 1);
                i += 2;
                col += 2;
            }
            '&' => {
                mk!(TokKind::Amp, c0, col);
                i += 1;
                col += 1;
            }
            '|' if i + 1 < n && chars[i + 1] == '|' => {
                mk!(TokKind::PipePipe, c0, col + 1);
                i += 2;
                col += 2;
            }
            '|' => {
                mk!(TokKind::Pipe, c0, col);
                i += 1;
                col += 1;
            }
            '^' => {
                mk!(TokKind::Caret, c0, col);
                i += 1;
                col += 1;
            }
            '<' if i + 1 < n && chars[i + 1] == '<' => {
                mk!(TokKind::Shl, c0, col + 1);
                i += 2;
                col += 2;
            }
            '<' if i + 1 < n && chars[i + 1] == '=' => {
                mk!(TokKind::Le, c0, col + 1);
                i += 2;
                col += 2;
            }
            '<' => {
                mk!(TokKind::Lt, c0, col);
                i += 1;
                col += 1;
            }
            '>' if i + 1 < n && chars[i + 1] == '>' => {
                mk!(TokKind::Shr, c0, col + 1);
                i += 2;
                col += 2;
            }
            '>' if i + 1 < n && chars[i + 1] == '=' => {
                mk!(TokKind::Ge, c0, col + 1);
                i += 2;
                col += 2;
            }
            '>' => {
                mk!(TokKind::Gt, c0, col);
                i += 1;
                col += 1;
            }
            '=' if i + 1 < n && chars[i + 1] == '=' => {
                mk!(TokKind::EqEq, c0, col + 1);
                i += 2;
                col += 2;
            }
            '=' => {
                mk!(TokKind::Assign, c0, col);
                i += 1;
                col += 1;
            }
            '!' if i + 1 < n && chars[i + 1] == '=' => {
                mk!(TokKind::NotEq, c0, col + 1);
                i += 2;
                col += 2;
            }
            '!' => {
                mk!(TokKind::Bang, c0, col);
                i += 1;
                col += 1;
            }
            '~' => {
                mk!(TokKind::Tilde, c0, col);
                i += 1;
                col += 1;
            }
            '"' => {
                i += 1;
                col += 1;
                let start = i;
                let mut s = String::new();
                while i < n && chars[i] != '"' {
                    if chars[i] == '\\' && i + 1 < n {
                        let e = chars[i + 1];
                        s.push(match e {
                            'n' => '\n',
                            't' => '\t',
                            '"' => '"',
                            '\\' => '\\',
                            other => other,
                        });
                        i += 2;
                        col += 2;
                    } else {
                        if chars[i] == '\n' {
                            return Err(LexError {
                                line,
                                col,
                                msg: "unterminated string".into(),
                            });
                        }
                        s.push(chars[i]);
                        i += 1;
                        col += 1;
                    }
                }
                if i >= n {
                    return Err(LexError {
                        line,
                        col,
                        msg: "unterminated string".into(),
                    });
                }
                i += 1; // closing quote
                col += 1;
                let _ = start;
                mk!(TokKind::Str(s), c0, col - 1);
            }
            '0' if i + 1 < n && (chars[i + 1] == 'x' || chars[i + 1] == 'X') => {
                i += 2;
                col += 2;
                let start = i;
                while i < n && chars[i].is_ascii_hexdigit() {
                    i += 1;
                    col += 1;
                }
                if start == i {
                    return Err(LexError {
                        line,
                        col,
                        msg: "hex literal needs at least one digit".into(),
                    });
                }
                let digits: String = chars[start..i].iter().collect();
                let mut suffix: Option<String> = None;
                if i < n && chars[i] == 'u' {
                    let s0 = i;
                    i += 1;
                    col += 1;
                    while i < n && chars[i].is_ascii_digit() {
                        i += 1;
                        col += 1;
                    }
                    suffix = Some(chars[s0..i].iter().collect());
                }
                mk!(TokKind::HexInt(digits, suffix), c0, col - 1);
            }
            c if c.is_ascii_digit() => {
                let start = i;
                while i < n && chars[i].is_ascii_digit() {
                    i += 1;
                    col += 1;
                }
                let digits: String = chars[start..i].iter().collect();
                let mut suffix: Option<String> = None;
                if i < n && chars[i] == 'u' {
                    let s0 = i;
                    i += 1;
                    col += 1;
                    while i < n && chars[i].is_ascii_digit() {
                        i += 1;
                        col += 1;
                    }
                    suffix = Some(chars[s0..i].iter().collect());
                }
                mk!(TokKind::Int(digits, suffix), c0, col - 1);
            }
            c if c.is_ascii_alphabetic() || c == '_' => {
                let start = i;
                while i < n && (chars[i].is_ascii_alphanumeric() || chars[i] == '_') {
                    i += 1;
                    col += 1;
                }
                let word: String = chars[start..i].iter().collect();
                let kind = match word.as_str() {
                    "param" => TokKind::Param,
                    "let" => TokKind::Let,
                    "if" => TokKind::If,
                    "else" => TokKind::Else,
                    "while" => TokKind::While,
                    "assert" => TokKind::Assert,
                    "assume" => TokKind::Assume,
                    "true" => TokKind::True,
                    "false" => TokKind::False,
                    _ => TokKind::Ident(word),
                };
                mk!(kind, c0, col - 1);
            }
            other => {
                return Err(LexError {
                    line,
                    col,
                    msg: format!("unexpected character {other:?}"),
                });
            }
        }
    }

    toks.push(Token {
        kind: TokKind::Eof,
        span: (col, col),
        line,
    });
    Ok(toks)
}
