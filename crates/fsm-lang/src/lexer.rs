use crate::error::{BuildError, BuildErrorKind};
use crate::token::{Token, TokenKind};

pub struct Lexer<'a> {
    src: &'a str,
    chars: Vec<(usize, char)>,
    idx: usize,
}

impl<'a> Lexer<'a> {
    pub fn new(src: &'a str) -> Self {
        Lexer {
            src,
            chars: src.char_indices().collect(),
            idx: 0,
        }
    }

    fn peek(&self) -> Option<char> {
        self.chars.get(self.idx).map(|(_, c)| *c)
    }

    fn peek2(&self) -> Option<char> {
        self.chars.get(self.idx + 1).map(|(_, c)| *c)
    }

    fn bump(&mut self) -> Option<(usize, char)> {
        let t = self.chars.get(self.idx).copied();
        if t.is_some() {
            self.idx += 1;
        }
        t
    }

    fn lex_err(&self, pos: usize, msg: impl Into<String>) -> BuildError {
        BuildError::new(BuildErrorKind::Lex, msg).with_source_pos(pos, self.line_col(pos))
    }

    fn line_col(&self, pos: usize) -> (usize, usize) {
        let mut line = 1usize;
        let mut col = 1usize;
        for (i, c) in self.src.char_indices() {
            if i >= pos {
                break;
            }
            if c == '\n' {
                line += 1;
                col = 1;
            } else {
                col += 1;
            }
        }
        (line, col)
    }

    pub fn tokenize(mut self) -> Result<Vec<Token>, BuildError> {
        let mut out = Vec::new();
        loop {
            // skip whitespace and `//` line comments
            while let Some(c) = self.peek() {
                if c.is_whitespace() {
                    self.bump();
                } else if c == '/' && self.peek2() == Some('/') {
                    while let Some(c) = self.peek() {
                        self.bump();
                        if c == '\n' {
                            break;
                        }
                    }
                } else {
                    break;
                }
            }
            let Some((pos, c)) = self.bump() else {
                out.push(Token {
                    kind: TokenKind::Eof,
                    pos: self.src.len(),
                });
                break;
            };
            let mut kind = match c {
                '(' => TokenKind::LParen,
                ')' => TokenKind::RParen,
                '{' => TokenKind::LBrace,
                '}' => TokenKind::RBrace,
                '[' => TokenKind::LBracket,
                ']' => TokenKind::RBracket,
                ',' => TokenKind::Comma,
                ';' => TokenKind::Semicolon,
                '.' => {
                    if self.peek() == Some('.') {
                        self.bump();
                        TokenKind::DotDot
                    } else {
                        TokenKind::Dot
                    }
                }
                ':' => {
                    if self.peek() == Some('=') {
                        self.bump();
                        TokenKind::Assign
                    } else {
                        TokenKind::Colon
                    }
                }
                '+' => TokenKind::Plus,
                '*' => TokenKind::Star,
                '/' => TokenKind::Slash,
                '-' => TokenKind::Minus,
                '<' => {
                    if self.peek() == Some('=') {
                        self.bump();
                        TokenKind::Le
                    } else {
                        TokenKind::Lt
                    }
                }
                '>' => {
                    if self.peek() == Some('=') {
                        self.bump();
                        TokenKind::Ge
                    } else {
                        TokenKind::Gt
                    }
                }
                '=' => {
                    if self.peek() == Some('=') {
                        self.bump();
                        TokenKind::EqEq
                    } else {
                        return Err(self.lex_err(
                            pos,
                            "single '=' is not valid; use '==' for equality or ':=' for assignment",
                        ));
                    }
                }
                '!' => {
                    if self.peek() == Some('=') {
                        self.bump();
                        TokenKind::Neq
                    } else {
                        TokenKind::Bang
                    }
                }
                '&' => {
                    if self.peek() == Some('&') {
                        self.bump();
                        TokenKind::And
                    } else {
                        return Err(self.lex_err(pos, "logical AND is written '&&'"));
                    }
                }
                '|' => {
                    if self.peek() == Some('|') {
                        self.bump();
                        TokenKind::Or
                    } else {
                        return Err(self.lex_err(pos, "logical OR is written '||'"));
                    }
                }
                c0 if c0.is_ascii_digit() => {
                    let mut s = String::from(c0);
                    while let Some(d) = self.peek() {
                        if d.is_ascii_digit() {
                            s.push(d);
                            self.bump();
                        } else {
                            break;
                        }
                    }
                    match s.parse::<i64>() {
                        Ok(i) => TokenKind::Int(i),
                        Err(_) => {
                            return Err(
                                self.lex_err(pos, format!("integer literal '{s}' overflows i64"))
                            );
                        }
                    }
                }
                c0 if c0.is_alphabetic() || c0 == '_' => {
                    let mut s = String::from(c0);
                    while let Some(d) = self.peek() {
                        if d.is_alphanumeric() || d == '_' {
                            s.push(d);
                            self.bump();
                        } else {
                            break;
                        }
                    }
                    keyword_or_ident(&s)
                }
                _ => return Err(self.lex_err(pos, format!("unexpected character {c:?}"))),
            };
            // unary `not` keyword
            if matches!(kind, TokenKind::KwNot) {
                kind = TokenKind::Bang;
            }
            out.push(Token { kind, pos });
        }
        Ok(out)
    }
}

fn keyword_or_ident(s: &str) -> TokenKind {
    match s {
        "system" => TokenKind::KwSystem,
        "var" => TokenKind::KwVar,
        "bool" => TokenKind::KwBool,
        "int" => TokenKind::KwInt,
        "enum" => TokenKind::KwEnum,
        "init" => TokenKind::KwInit,
        "transition" => TokenKind::KwTransition,
        "guard" => TokenKind::KwGuard,
        "then" => TokenKind::KwThen,
        "terminal" => TokenKind::KwTerminal,
        "true" => TokenKind::KwTrue,
        "false" => TokenKind::KwFalse,
        "if" => TokenKind::KwIf,
        "else" => TokenKind::KwElse,
        "not" => TokenKind::KwNot,
        "mod" => TokenKind::KwMod,
        other => TokenKind::Ident(other.to_string()),
    }
}
