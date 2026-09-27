//! Recursive-descent parser.
//!
//! Grammar (earlier line binds tighter; all binary operators are
//! right-associative, matching the conventional reading of `->` and `<->`):
//!
//! ```text
//! expr   := equiv
//! equiv  := implies (("<->" ) implies)*
//! implies:= xor     ("->"     xor)*
//! xor    := or      ("^"      or)*
//! or     := and     ("||"     and)*
//! and    := unary   ("&&"     unary)*
//! unary  := "!" unary | atom
//! atom   := "true" | "false" | ident | "(" expr ")"
//! ```

use super::ast::{BinOp, Expr};
use super::lexer::{lex, Span, Tok, Token};

/// Parse failure: an unexpected token and what was expected instead.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ParseError {
    pub span: Span,
    pub found: String,
    pub expected: String,
    pub message: String,
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "parse error at bytes {}..{}: found {} but expected {} ({})",
            self.span.start, self.span.end, self.found, self.expected, self.message
        )
    }
}
impl std::error::Error for ParseError {}

/// Parse one Boolean expression; the whole input must be consumed.
pub fn parse(src: &str) -> Result<Expr, ParseError> {
    let tokens = lex(src).map_err(|e| ParseError {
        span: Span {
            start: e.pos,
            end: e.pos + e.found.map_or(0, |c| c.len_utf8()),
        },
        found: e
            .found
            .map_or_else(|| "end of input".into(), |c| c.to_string()),
        expected: "a valid token".into(),
        message: e.message,
    })?;
    let mut p = Parser {
        src,
        tokens,
        pos: 0,
    };
    let e = p.parse_equiv()?;
    p.expect(Tok::Eof, "end of input")?;
    Ok(e)
}

struct Parser<'a> {
    src: &'a str,
    tokens: Vec<Token>,
    pos: usize,
}

impl<'a> Parser<'a> {
    fn peek(&self) -> &Token {
        &self.tokens[self.pos]
    }

    fn advance(&mut self) -> Token {
        let t = self.tokens[self.pos].clone();
        if t.kind != Tok::Eof {
            self.pos += 1;
        }
        t
    }

    fn expect(&mut self, kind: Tok, what: &str) -> Result<Token, ParseError> {
        if self.peek().kind == kind {
            Ok(self.advance())
        } else {
            Err(self.unexpected(what))
        }
    }

    fn unexpected(&self, what: &str) -> ParseError {
        let t = self.peek().clone();
        let found = match t.kind {
            Tok::Eof => "end of input".to_string(),
            _ => format!("`{}`", &self.src[t.span.start..t.span.end]),
        };
        ParseError {
            span: t.span,
            found,
            expected: what.to_string(),
            message: "expression is incomplete or malformed".into(),
        }
    }

    fn binary_level(
        &mut self,
        op_tok: Tok,
        op: BinOp,
        sub: fn(&mut Self) -> Result<Expr, ParseError>,
    ) -> Result<Expr, ParseError> {
        let mut lhs = sub(self)?;
        while self.peek().kind == op_tok {
            self.advance();
            let rhs = sub(self)?;
            lhs = Expr::Binary {
                op,
                lhs: Box::new(lhs),
                rhs: Box::new(rhs),
            };
        }
        Ok(lhs)
    }

    fn parse_equiv(&mut self) -> Result<Expr, ParseError> {
        // Right-associative: parse a implies, then on `<->` recursively parse.
        let lhs = self.parse_implies()?;
        if self.peek().kind == Tok::Equiv {
            self.advance();
            let rhs = self.parse_equiv()?;
            return Ok(Expr::Binary {
                op: BinOp::Equiv,
                lhs: Box::new(lhs),
                rhs: Box::new(rhs),
            });
        }
        Ok(lhs)
    }

    fn parse_implies(&mut self) -> Result<Expr, ParseError> {
        let lhs = self.parse_xor()?;
        if self.peek().kind == Tok::Implies {
            self.advance();
            let rhs = self.parse_implies()?;
            return Ok(Expr::Binary {
                op: BinOp::Implies,
                lhs: Box::new(lhs),
                rhs: Box::new(rhs),
            });
        }
        Ok(lhs)
    }

    fn parse_xor(&mut self) -> Result<Expr, ParseError> {
        self.binary_level(Tok::Xor, BinOp::Xor, Parser::parse_or)
    }

    fn parse_or(&mut self) -> Result<Expr, ParseError> {
        self.binary_level(Tok::Or, BinOp::Or, Parser::parse_and)
    }

    fn parse_and(&mut self) -> Result<Expr, ParseError> {
        self.binary_level(Tok::And, BinOp::And, Parser::parse_unary)
    }

    fn parse_unary(&mut self) -> Result<Expr, ParseError> {
        if self.peek().kind == Tok::Not {
            self.advance();
            Ok(Expr::Not(Box::new(self.parse_unary()?)))
        } else {
            self.parse_atom()
        }
    }

    fn parse_atom(&mut self) -> Result<Expr, ParseError> {
        let t = self.peek().clone();
        match t.kind {
            Tok::KwTrue => {
                self.advance();
                Ok(Expr::Const(true))
            }
            Tok::KwFalse => {
                self.advance();
                Ok(Expr::Const(false))
            }
            Tok::Ident => {
                self.advance();
                Ok(Expr::Var(self.src[t.span.start..t.span.end].to_string()))
            }
            Tok::LParen => {
                self.advance();
                let inner = self.parse_equiv()?;
                self.expect(Tok::RParen, "closing parenthesis `)`")?;
                Ok(inner)
            }
            _ => Err(self.unexpected("`true`, `false`, a variable, `!`, or `(`")),
        }
    }
}
