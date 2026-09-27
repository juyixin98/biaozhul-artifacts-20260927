//! Recursive-descent parser. Whitespace-insensitive; every AST node carries
//! the byte span it occupies so reports can point back at the source.
use crate::ast::*;
use crate::lexer::{lex, Token, TokenKind};
use crate::span::Span;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ParseError {
    pub message: String,
    pub span: Span,
}

pub fn parse(source: &str) -> Result<Program, ParseError> {
    let tokens = lex(source).map_err(|e| ParseError {
        message: e.message,
        span: e.span,
    })?;
    let mut p = Parser { tokens, pos: 0 };
    p.parse_program()
}

struct Parser {
    tokens: Vec<Token>,
    pos: usize,
}

impl Parser {
    fn peek(&self) -> &TokenKind {
        &self.tokens[self.pos].kind
    }
    fn peek_token(&self) -> &Token {
        &self.tokens[self.pos]
    }
    fn at(&self, k: &TokenKind) -> bool {
        std::mem::discriminant(self.peek()) == std::mem::discriminant(k)
    }
    fn bump(&mut self) -> Token {
        let t = self.tokens[self.pos].clone();
        if !matches!(t.kind, TokenKind::Eof) {
            self.pos += 1;
        }
        t
    }

    fn err(&self, message: impl Into<String>, span: Span) -> ParseError {
        ParseError {
            message: message.into(),
            span,
        }
    }
    fn err_here(&self, message: impl Into<String>) -> ParseError {
        let t = self.peek_token();
        self.err(message, t.span)
    }

    fn expect(&mut self, k: TokenKind, what: &str) -> Result<Token, ParseError> {
        if self.at(&k) {
            Ok(self.bump())
        } else {
            Err(self.err_here(format!("expected {what}")))
        }
    }

    fn parse_program(&mut self) -> Result<Program, ParseError> {
        let start_tok = self.peek_token().clone();
        let mut decls = Vec::new();
        loop {
            match self.peek() {
                TokenKind::Input => decls.push(self.parse_input_decl()?),
                TokenKind::Const => decls.push(self.parse_const_decl()?),
                TokenKind::Array => decls.push(self.parse_array_decl()?),
                _ => break,
            }
        }
        let body = self.parse_block()?;
        if !self.at(&TokenKind::Eof) {
            return Err(self.err_here("unexpected tokens after program body"));
        }
        let span = start_tok.span.merge(body.span);
        Ok(Program { decls, body, span })
    }

    fn parse_input_decl(&mut self) -> Result<Decl, ParseError> {
        let kw = self.bump(); // input
        let name_tok = self.expect_ident()?;
        self.expect(TokenKind::LBracket, "'[' after input name")?;
        let lo = self.parse_bound_lit()?;
        self.expect(TokenKind::Colon, "':' between input bounds")?;
        let hi = self.parse_bound_lit()?;
        self.expect(TokenKind::RBracket, "']' after input bounds")?;
        let semi = self.expect(TokenKind::Semicolon, "';' after input declaration")?;
        if lo > hi {
            return Err(self.err(
                format!("input bounds [{lo}, {hi}] are empty (lower bound exceeds upper bound)"),
                kw.span,
            ));
        }
        Ok(Decl::Input {
            name: name_tok_text(&name_tok),
            lo,
            hi,
            span: kw.span.merge(semi.span),
            name_span: name_tok.span,
        })
    }

    /// Bounds accept any i64 literal *and* the magnitude token for i64::MIN,
    /// so `input x [-9223372036854775808: 0]` parses.
    fn parse_bound_lit(&mut self) -> Result<i64, ParseError> {
        match self.peek().clone() {
            TokenKind::Int(v) => {
                self.bump();
                Ok(v)
            }
            TokenKind::Minus => {
                let t = self.bump();
                match self.peek().clone() {
                    TokenKind::Int(v) => {
                        self.bump();
                        v.checked_neg().ok_or_else(|| {
                            self.err(
                                "negation of this literal overflows bounded i64",
                                t.span,
                            )
                        })
                    }
                    TokenKind::MinMag => {
                        self.bump();
                        Ok(i64::MIN)
                    }
                    _ => Err(self.err_here("expected integer literal bound")),
                }
            }
            TokenKind::MinMag => Err(self.err_here(
                "9223372036854775808 is not representable; write -9223372036854775808",
            )),
            _ => Err(self.err_here("expected integer literal bound")),
        }
    }

    fn parse_const_decl(&mut self) -> Result<Decl, ParseError> {
        let kw = self.bump(); // const
        let name_tok = self.expect_ident()?;
        self.expect(TokenKind::Assign, "'=' in const declaration")?;
        let value = self.parse_bound_lit()?;
        let semi = self.expect(TokenKind::Semicolon, "';' after const declaration")?;
        Ok(Decl::Const {
            name: name_tok_text(&name_tok),
            value,
            span: kw.span.merge(semi.span),
            name_span: name_tok.span,
        })
    }

    fn parse_array_decl(&mut self) -> Result<Decl, ParseError> {
        let kw = self.bump(); // array
        let name_tok = self.expect_ident()?;
        self.expect(TokenKind::LBracket, "'[' after array name")?;
        let len_tok = self.bump();
        let len = match len_tok.kind {
            TokenKind::Int(v) if v >= 0 => v as u64,
            _ => {
                return Err(self.err(
                    "array length must be a non-negative integer literal",
                    len_tok.span,
                ))
            }
        };
        self.expect(TokenKind::RBracket, "']' after array length")?;
        let semi = self.expect(TokenKind::Semicolon, "';' after array declaration")?;
        Ok(Decl::Array {
            name: name_tok_text(&name_tok),
            len,
            span: kw.span.merge(semi.span),
            name_span: name_tok.span,
        })
    }

    fn expect_ident(&mut self) -> Result<Token, ParseError> {
        if let TokenKind::Ident(_) = self.peek() {
            Ok(self.bump())
        } else {
            Err(self.err_here("expected identifier"))
        }
    }

    fn parse_block(&mut self) -> Result<Block, ParseError> {
        let lb = self.expect(TokenKind::LBrace, "'{{' to open a block")?;
        let mut stmts = Vec::new();
        while !self.at(&TokenKind::RBrace) && !self.at(&TokenKind::Eof) {
            stmts.push(self.parse_stmt()?);
        }
        let rb = self.expect(TokenKind::RBrace, "'}}' to close a block")?;
        Ok(Block {
            stmts,
            span: lb.span.merge(rb.span),
        })
    }

    fn parse_stmt(&mut self) -> Result<Stmt, ParseError> {
        match self.peek() {
            TokenKind::LBrace => Ok(Stmt::Block(self.parse_block()?)),
            TokenKind::If => self.parse_if(),
            TokenKind::While => self.parse_while(),
            TokenKind::Assert => self.parse_assert(),
            TokenKind::Skip => {
                let t = self.bump();
                let semi = self.expect(TokenKind::Semicolon, "';' after skip")?;
                Ok(Stmt::Skip {
                    span: t.span.merge(semi.span),
                })
            }
            _ => self.parse_assign(),
        }
    }

    fn parse_if(&mut self) -> Result<Stmt, ParseError> {
        let kw = self.bump(); // if
        self.expect(TokenKind::LParen, "'(' after if")?;
        let cond = self.parse_expr()?;
        self.expect(TokenKind::RParen, "')' after if condition")?;
        let then = self.parse_stmt()?;
        let mut span_end = then.span();
        let otherwise = if self.at(&TokenKind::Else) {
            self.bump();
            let e = self.parse_stmt()?;
            span_end = e.span();
            Some(Box::new(e))
        } else {
            None
        };
        Ok(Stmt::If {
            cond,
            then: Box::new(then),
            otherwise,
            span: kw.span.merge(span_end),
        })
    }

    fn parse_while(&mut self) -> Result<Stmt, ParseError> {
        let kw = self.bump(); // while
        self.expect(TokenKind::LParen, "'(' after while")?;
        let cond = self.parse_expr()?;
        self.expect(TokenKind::RParen, "')' after while condition")?;
        let body = self.parse_stmt()?;
        let span = kw.span.merge(body.span());
        Ok(Stmt::While {
            cond,
            body: Box::new(body),
            span,
        })
    }

    fn parse_assert(&mut self) -> Result<Stmt, ParseError> {
        let kw = self.bump(); // assert
        self.expect(TokenKind::LParen, "'(' after assert")?;
        let cond = self.parse_expr()?;
        self.expect(TokenKind::RParen, "')' after assert condition")?;
        let semi = self.expect(TokenKind::Semicolon, "';' after assert")?;
        Ok(Stmt::Assert {
            cond,
            span: kw.span.merge(semi.span),
        })
    }

    fn parse_assign(&mut self) -> Result<Stmt, ParseError> {
        let target = self.parse_lvalue()?;
        self.expect(TokenKind::Assign, "'=' in assignment")?;
        let value = self.parse_expr()?;
        let semi = self.expect(TokenKind::Semicolon, "';' after assignment")?;
        let span = target.span.merge(semi.span);
        Ok(Stmt::Assign {
            target,
            value,
            span,
        })
    }

    fn parse_lvalue(&mut self) -> Result<Lvalue, ParseError> {
        let name_tok = self.expect_ident()?;
        if self.at(&TokenKind::LBracket) {
            self.bump();
            let index = self.parse_expr()?;
            let rb = self.expect(TokenKind::RBracket, "']' after array index")?;
            Ok(Lvalue {
                name: name_tok_text(&name_tok),
                index: Some(index),
                span: name_tok.span.merge(rb.span),
                name_span: name_tok.span,
            })
        } else {
            Ok(Lvalue {
                name: name_tok_text(&name_tok),
                index: None,
                span: name_tok.span,
                name_span: name_tok.span,
            })
        }
    }

    // Expression grammar (lowest precedence first):
    //   or   := and ('||' and)*
    //   and  := cmp ('&&' cmp)*
    //   cmp  := add (('==' | '!=' | '<' | '<=' | '>' | '>=') add)*
    //   add  := mul (('+' | '-') mul)*
    //   mul  := unary (('*' | '/' | '%') unary)*
    //   unary:= ('-' | '!') unary | postfix
    //   atom := INT | IDENT ('[' expr ']')? | '(' expr ')'
    fn parse_expr(&mut self) -> Result<Expr, ParseError> {
        self.parse_or()
    }

    fn bin_loop(
        &mut self,
        sub: fn(&mut Parser) -> Result<Expr, ParseError>,
        ops: &[(TokenKind, BinOp)],
    ) -> Result<Expr, ParseError> {
        let mut lhs = sub(self)?;
        loop {
            let op = ops.iter().find_map(|(tk, op)| {
                if std::mem::discriminant(self.peek()) == std::mem::discriminant(tk) {
                    Some(*op)
                } else {
                    None
                }
            });
            if let Some(op) = op {
                let op_tok = self.bump();
                let rhs = sub(self)?;
                let span = lhs.span.merge(rhs.span);
                lhs = Expr {
                    kind: ExprKind::Binary {
                        op,
                        lhs: Box::new(lhs),
                        rhs: Box::new(rhs),
                    },
                    span,
                };
                let _ = op_tok;
            } else {
                break;
            }
        }
        Ok(lhs)
    }

    fn parse_or(&mut self) -> Result<Expr, ParseError> {
        self.bin_loop(Self::parse_and, &[(TokenKind::PipePipe, BinOp::Or)])
    }
    fn parse_and(&mut self) -> Result<Expr, ParseError> {
        self.bin_loop(Self::parse_cmp, &[(TokenKind::AmpAmp, BinOp::And)])
    }
    fn parse_cmp(&mut self) -> Result<Expr, ParseError> {
        self.bin_loop(
            Self::parse_add,
            &[
                (TokenKind::Lt, BinOp::Lt),
                (TokenKind::Le, BinOp::Le),
                (TokenKind::Gt, BinOp::Gt),
                (TokenKind::Ge, BinOp::Ge),
                (TokenKind::EqEq, BinOp::Eq),
                (TokenKind::NotEq, BinOp::Ne),
            ],
        )
    }
    fn parse_add(&mut self) -> Result<Expr, ParseError> {
        self.bin_loop(
            Self::parse_mul,
            &[(TokenKind::Plus, BinOp::Add), (TokenKind::Minus, BinOp::Sub)],
        )
    }
    fn parse_mul(&mut self) -> Result<Expr, ParseError> {
        self.bin_loop(
            Self::parse_unary,
            &[
                (TokenKind::Star, BinOp::Mul),
                (TokenKind::Slash, BinOp::Div),
                (TokenKind::Percent, BinOp::Mod),
            ],
        )
    }

    fn parse_unary(&mut self) -> Result<Expr, ParseError> {
        match self.peek() {
            TokenKind::Minus => {
                let t = self.bump();
                // `-9223372036854775808` is the literal i64::MIN.
                if let TokenKind::MinMag = self.peek() {
                    let mag = self.bump();
                    let span = t.span.merge(mag.span);
                    return Ok(Expr {
                        kind: ExprKind::Int(i64::MIN),
                        span,
                    });
                }
                let inner = self.parse_unary()?;
                let span = t.span.merge(inner.span);
                Ok(Expr {
                    kind: ExprKind::Unary {
                        op: UnOp::Neg,
                        inner: Box::new(inner),
                    },
                    span,
                })
            }
            TokenKind::Bang => {
                let t = self.bump();
                let inner = self.parse_unary()?;
                let span = t.span.merge(inner.span);
                Ok(Expr {
                    kind: ExprKind::Unary {
                        op: UnOp::Not,
                        inner: Box::new(inner),
                    },
                    span,
                })
            }
            _ => self.parse_atom(),
        }
    }

    fn parse_atom(&mut self) -> Result<Expr, ParseError> {
        let t = self.peek_token().clone();
        match t.kind {
            TokenKind::Int(v) => {
                self.bump();
                Ok(Expr {
                    kind: ExprKind::Int(v),
                    span: t.span,
                })
            }
            // Only legal as the operand of a unary minus consumed in
            // parse_unary; keeps the literal MIN representable.
            TokenKind::MinMag => {
                self.bump();
                Err(self.err(
                    "9223372036854775808 is not representable; write -9223372036854775808",
                    t.span,
                ))
            }
            TokenKind::Ident(name) => {
                self.bump();
                if self.at(&TokenKind::LBracket) {
                    self.bump();
                    let index = self.parse_expr()?;
                    let rb = self.expect(TokenKind::RBracket, "']' after array index")?;
                    Ok(Expr {
                        kind: ExprKind::ArrayRead {
                            name,
                            index: Box::new(index),
                        },
                        span: t.span.merge(rb.span),
                    })
                } else {
                    Ok(Expr {
                        kind: ExprKind::Var(name),
                        span: t.span,
                    })
                }
            }
            TokenKind::LParen => {
                self.bump();
                let e = self.parse_expr()?;
                self.expect(TokenKind::RParen, "')' to close parenthesised expression")?;
                // Parentheses do not widen the node span beyond the inner
                // expression; error locations stay on the operator.
                Ok(e)
            }
            _ => Err(self.err_here("expected expression")),
        }
    }
}

fn name_tok_text(t: &Token) -> String {
    match &t.kind {
        TokenKind::Ident(s) => s.clone(),
        _ => unreachable!("expect_ident returns only ident tokens"),
    }
}

/// Render an error together with its source line for CLI/report use.
pub fn render_source_line(source: &str, span: Span) -> String {
    let line_start = source[..span.start.offset.min(source.len())]
        .rfind('\n')
        .map(|i| i + 1)
        .unwrap_or(0);
    let line_end = source[line_start..]
        .find('\n')
        .map(|i| line_start + i)
        .unwrap_or(source.len());
    let line = &source[line_start..line_end];
    let pad = " ".repeat((span.start.column as usize).saturating_sub(1));
    let carets = "^".repeat((span.end.offset.saturating_sub(span.start.offset)).max(1));
    format!(
        "{line}\n{pad}{carets}  (line {}, column {})",
        span.start.line, span.start.column
    )
}
