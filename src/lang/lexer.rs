use super::ast::Span;
use super::error::LangError;

#[derive(Debug, Clone, PartialEq)]
pub enum Tok {
    Ident(String),
    Int(i64),
    // keywords
    Let,
    Array,
    If,
    Else,
    While,
    Assert,
    // punctuation / operators
    Assign, // :=
    LBracket,
    RBracket,
    LBrace,
    RBrace,
    LParen,
    RParen,
    Semicolon,
    Colon,
    Comma,
    Plus,
    Minus,
    Star,
    Bang,
    Lt,
    Le,
    EqEq,
    Ne,
    Ge,
    Gt,
    AndAnd,
    OrOr,
    Eof,
}

#[derive(Debug, Clone)]
pub struct Token {
    pub tok: Tok,
    pub span: Span,
}

pub fn lex(src: &str) -> Result<Vec<Token>, LangError> {
    let bytes = src.as_bytes();
    let mut tokens = Vec::new();
    let mut i = 0usize;
    let mut line = 1u32;
    let mut col = 1u32;
    let start_line = 1u32;
    let start_col = 1u32;

    fn mkspan(line: u32, col: u32, offset: usize, len: usize) -> Span {
        Span::new(line, col, offset as u32, len as u32)
    }

    while i < bytes.len() {
        let c = bytes[i] as char;
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
            '#' => {
                // line comment
                while i < bytes.len() && bytes[i] != b'\n' {
                    i += 1;
                }
            }
            'a'..='z' | 'A'..='Z' | '_' => {
                let (tok_line, tok_col, start) = (line, col, i);
                while i < bytes.len()
                    && (matches!(bytes[i] as char, 'a'..='z' | 'A'..='Z' | '0'..='9' | '_'))
                {
                    i += 1;
                    col += 1;
                }
                let word = &src[start..i];
                let tok = match word {
                    "let" => Tok::Let,
                    "array" => Tok::Array,
                    "if" => Tok::If,
                    "else" => Tok::Else,
                    "while" => Tok::While,
                    "assert" => Tok::Assert,
                    _ => Tok::Ident(word.to_string()),
                };
                tokens.push(Token {
                    tok,
                    span: mkspan(tok_line, tok_col, start, i - start),
                });
            }
            '0'..='9' => {
                let (tok_line, tok_col, start) = (line, col, i);
                while i < bytes.len() && bytes[i].is_ascii_digit() {
                    i += 1;
                    col += 1;
                }
                let word = &src[start..i];
                let value: i64 = word.parse().map_err(|_| {
                    LangError::new(
                        format!("integer literal `{word}` is outside the i64 range"),
                        mkspan(tok_line, tok_col, start, i - start),
                    )
                })?;
                tokens.push(Token {
                    tok: Tok::Int(value),
                    span: mkspan(tok_line, tok_col, start, i - start),
                });
            }
            ':' => {
                if bytes.get(i + 1) == Some(&b'=') {
                    tokens.push(Token {
                        tok: Tok::Assign,
                        span: mkspan(line, col, i, 2),
                    });
                    i += 2;
                    col += 2;
                } else {
                    tokens.push(Token {
                        tok: Tok::Colon,
                        span: mkspan(line, col, i, 1),
                    });
                    i += 1;
                    col += 1;
                }
            }
            '!' => {
                if bytes.get(i + 1) == Some(&b'=') {
                    tokens.push(Token {
                        tok: Tok::Ne,
                        span: mkspan(line, col, i, 2),
                    });
                    i += 2;
                    col += 2;
                } else {
                    tokens.push(Token {
                        tok: Tok::Bang,
                        span: mkspan(line, col, i, 1),
                    });
                    i += 1;
                    col += 1;
                }
            }
            '<' => {
                if bytes.get(i + 1) == Some(&b'=') {
                    tokens.push(Token {
                        tok: Tok::Le,
                        span: mkspan(line, col, i, 2),
                    });
                    i += 2;
                    col += 2;
                } else {
                    tokens.push(Token {
                        tok: Tok::Lt,
                        span: mkspan(line, col, i, 1),
                    });
                    i += 1;
                    col += 1;
                }
            }
            '>' => {
                if bytes.get(i + 1) == Some(&b'=') {
                    tokens.push(Token {
                        tok: Tok::Ge,
                        span: mkspan(line, col, i, 2),
                    });
                    i += 2;
                    col += 2;
                } else {
                    tokens.push(Token {
                        tok: Tok::Gt,
                        span: mkspan(line, col, i, 1),
                    });
                    i += 1;
                    col += 1;
                }
            }
            '=' => {
                if bytes.get(i + 1) == Some(&b'=') {
                    tokens.push(Token {
                        tok: Tok::EqEq,
                        span: mkspan(line, col, i, 2),
                    });
                    i += 2;
                    col += 2;
                } else {
                    return Err(LangError::new(
                        "unexpected `=`; did you mean `:=` for assignment or `==` for comparison?",
                        mkspan(line, col, i, 1),
                    ));
                }
            }
            '&' => {
                if bytes.get(i + 1) == Some(&b'&') {
                    tokens.push(Token {
                        tok: Tok::AndAnd,
                        span: mkspan(line, col, i, 2),
                    });
                    i += 2;
                    col += 2;
                } else {
                    return Err(LangError::new(
                        "unexpected `&`; did you mean `&&`?",
                        mkspan(line, col, i, 1),
                    ));
                }
            }
            '|' => {
                if bytes.get(i + 1) == Some(&b'|') {
                    tokens.push(Token {
                        tok: Tok::OrOr,
                        span: mkspan(line, col, i, 2),
                    });
                    i += 2;
                    col += 2;
                } else {
                    return Err(LangError::new(
                        "unexpected `|`; did you mean `||`?",
                        mkspan(line, col, i, 1),
                    ));
                }
            }
            '[' => {
                tokens.push(Token {
                    tok: Tok::LBracket,
                    span: mkspan(line, col, i, 1),
                });
                i += 1;
                col += 1;
            }
            ']' => {
                tokens.push(Token {
                    tok: Tok::RBracket,
                    span: mkspan(line, col, i, 1),
                });
                i += 1;
                col += 1;
            }
            '{' => {
                tokens.push(Token {
                    tok: Tok::LBrace,
                    span: mkspan(line, col, i, 1),
                });
                i += 1;
                col += 1;
            }
            '}' => {
                tokens.push(Token {
                    tok: Tok::RBrace,
                    span: mkspan(line, col, i, 1),
                });
                i += 1;
                col += 1;
            }
            '(' => {
                tokens.push(Token {
                    tok: Tok::LParen,
                    span: mkspan(line, col, i, 1),
                });
                i += 1;
                col += 1;
            }
            ')' => {
                tokens.push(Token {
                    tok: Tok::RParen,
                    span: mkspan(line, col, i, 1),
                });
                i += 1;
                col += 1;
            }
            ';' => {
                tokens.push(Token {
                    tok: Tok::Semicolon,
                    span: mkspan(line, col, i, 1),
                });
                i += 1;
                col += 1;
            }
            ',' => {
                tokens.push(Token {
                    tok: Tok::Comma,
                    span: mkspan(line, col, i, 1),
                });
                i += 1;
                col += 1;
            }
            '+' => {
                tokens.push(Token {
                    tok: Tok::Plus,
                    span: mkspan(line, col, i, 1),
                });
                i += 1;
                col += 1;
            }
            '-' => {
                tokens.push(Token {
                    tok: Tok::Minus,
                    span: mkspan(line, col, i, 1),
                });
                i += 1;
                col += 1;
            }
            '*' => {
                tokens.push(Token {
                    tok: Tok::Star,
                    span: mkspan(line, col, i, 1),
                });
                i += 1;
                col += 1;
            }
            other => {
                return Err(LangError::new(
                    format!("unexpected character `{other}`"),
                    mkspan(line, col, i, 1),
                ));
            }
        }
    }

    tokens.push(Token {
        tok: Tok::Eof,
        span: Span::new(line, col, i as u32, 0),
    });
    let _ = start_line;
    let _ = start_col;
    Ok(tokens)
}
