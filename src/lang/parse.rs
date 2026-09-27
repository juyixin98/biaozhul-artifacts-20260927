//! Recursive-descent parser.
//!
//! Grammar (see README for the human version):
//! ```text
//! program  := (param_decl)* stmt*
//! param    := 'param' ident ':' type ';'
//! type     := 'u8' | 'u16' | 'u32' | 'u64'
//! stmt     := let | assign | assert | assume | if | while | block
//! expr     := ternary-free; precedence: || && | == != < <= > >= | << >> |
//!             + - | * / % | unary | primary
//! ```

use super::ast::*;
use super::lex::{lex, TokKind, Token};
use super::types::Type;

#[derive(Debug)]
pub struct ParseError {
    pub line: u32,
    pub col: u32,
    pub msg: String,
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "line {} col {}: {}", self.line, self.col, self.msg)
    }
}
impl std::error::Error for ParseError {}

struct Parser {
    toks: Vec<Token>,
    pos: usize,
}

impl Parser {
    fn peek(&self) -> &TokKind {
        &self.toks[self.pos].kind
    }
    fn tok(&self) -> &Token {
        &self.toks[self.pos]
    }
    fn bump(&mut self) -> Token {
        let t = self.toks[self.pos].clone();
        if self.pos < self.toks.len() - 1 {
            self.pos += 1;
        }
        t
    }
    fn eat(&mut self, k: &TokKind) -> bool {
        if self.peek() == k {
            self.bump();
            true
        } else {
            false
        }
    }
    fn expect(&mut self, k: &TokKind, what: &str) -> Result<Token, ParseError> {
        if self.peek() == k {
            Ok(self.bump())
        } else {
            Err(self.err(format!("expected {what}, found {}", tok_name(self.peek()))))
        }
    }
    fn err(&self, msg: String) -> ParseError {
        let t = self.tok();
        ParseError {
            line: t.line,
            col: t.span.0,
            msg,
        }
    }

    fn parse_type(&mut self) -> Result<Type, ParseError> {
        let t = self.bump();
        match t.kind {
            TokKind::Ident(s) => match s.as_str() {
                "u8" => Ok(Type::U8),
                "u16" => Ok(Type::U16),
                "u32" => Ok(Type::U32),
                "u64" => Ok(Type::U64),
                _ => Err(ParseError {
                    line: t.line,
                    col: t.span.0,
                    msg: format!("unknown type `{s}` (expected u8/u16/u32/u64)"),
                }),
            },
            k => Err(ParseError {
                line: t.line,
                col: t.span.0,
                msg: format!("expected type, found {}", tok_name(&k)),
            }),
        }
    }

    fn program(&mut self) -> Result<Program, ParseError> {
        let mut params = Vec::new();
        let mut body = Vec::new();
        while *self.peek() != TokKind::Eof {
            if *self.peek() == TokKind::Param {
                self.bump();
                let name = self.expect_ident("parameter name")?;
                self.expect(&TokKind::Colon, "`:`")?;
                let ty = self.parse_type()?;
                self.expect(&TokKind::Semi, "`;`")?;
                params.push(Param { name, ty });
            } else {
                body.push(self.stmt()?);
            }
        }
        Ok(Program { params, body })
    }

    fn expect_ident(&mut self, what: &str) -> Result<String, ParseError> {
        match self.peek().clone() {
            TokKind::Ident(s) => {
                self.bump();
                Ok(s)
            }
            k => Err(self.err(format!("expected {what}, found {}", tok_name(&k)))),
        }
    }

    fn block(&mut self) -> Result<Vec<Stmt>, ParseError> {
        self.expect(&TokKind::LBrace, "`{`")?;
        let mut stmts = Vec::new();
        while *self.peek() != TokKind::RBrace && *self.peek() != TokKind::Eof {
            stmts.push(self.stmt()?);
        }
        self.expect(&TokKind::RBrace, "`}`")?;
        Ok(stmts)
    }

    fn stmt(&mut self) -> Result<Stmt, ParseError> {
        let start = self.tok().clone();
        let kind = match self.peek().clone() {
            TokKind::LBrace => StmtKind::If {
                // a bare block is desugared into if(true){...}else{}
                cond: Expr::new(ExprKind::BoolLit(true), span_of(&start, &start)),
                then: self.block()?,
                els: vec![],
            },
            TokKind::Let => {
                self.bump();
                let name = self.expect_ident("variable name")?;
                self.expect(&TokKind::Colon, "`:`")?;
                let ty = self.parse_type()?;
                self.expect(&TokKind::Assign, "`=`")?;
                let value = self.expr()?;
                self.expect(&TokKind::Semi, "`;`")?;
                StmtKind::Let { name, ty, value }
            }
            TokKind::Assert => {
                self.bump();
                self.expect(&TokKind::LParen, "`(`")?;
                let cond = self.expr()?;
                let message = if self.eat(&TokKind::Comma) {
                    match self.bump().kind {
                        TokKind::Str(s) => Some(s),
                        k => return Err(self.err(format!("expected message string, found {}", tok_name(&k)))),
                    }
                } else {
                    None
                };
                self.expect(&TokKind::RParen, "`)`")?;
                self.expect(&TokKind::Semi, "`;`")?;
                StmtKind::Assert { cond, message }
            }
            TokKind::Assume => {
                self.bump();
                self.expect(&TokKind::LParen, "`(`")?;
                let cond = self.expr()?;
                self.expect(&TokKind::RParen, "`)`")?;
                self.expect(&TokKind::Semi, "`;`")?;
                StmtKind::Assume { cond }
            }
            TokKind::If => {
                self.bump();
                self.expect(&TokKind::LParen, "`(`")?;
                let cond = self.expr()?;
                self.expect(&TokKind::RParen, "`)`")?;
                let then = self.block()?;
                let els = if self.eat(&TokKind::Else) {
                    if *self.peek() == TokKind::If {
                        vec![self.stmt()?]
                    } else {
                        self.block()?
                    }
                } else {
                    vec![]
                };
                StmtKind::If { cond, then, els }
            }
            TokKind::While => {
                self.bump();
                self.expect(&TokKind::LParen, "`(`")?;
                let cond = self.expr()?;
                self.expect(&TokKind::RParen, "`)`")?;
                let body = self.block()?;
                StmtKind::While { cond, body }
            }
            TokKind::Ident(_) => {
                let name = self.expect_ident("lvalue")?;
                self.expect(&TokKind::Assign, "`=`")?;
                let value = self.expr()?;
                self.expect(&TokKind::Semi, "`;`")?;
                StmtKind::Assign { name, value }
            }
            k => return Err(self.err(format!("expected statement, found {}", tok_name(&k)))),
        };
        let end = self.toks[self.pos.saturating_sub(1)].clone();
        Ok(Stmt {
            id: NodeId::placeholder(),
            kind,
            span: span_of(&start, &end),
        })
    }

    fn expr(&mut self) -> Result<Expr, ParseError> {
        self.parse_or()
    }

    fn binary_layer(
        &mut self,
        ops: &[(&TokKind, BinOp)],
        next: fn(&mut Self) -> Result<Expr, ParseError>,
    ) -> Result<Expr, ParseError> {
        let start = self.tok().clone();
        let mut left = next(self)?;
        loop {
            let op = ops.iter().find(|(t, _)| *t == self.peek());
            let Some((_, op)) = op else { break };
            let op = *op;
            let op_tok = self.bump();
            let right = next(self)?;
            let span = Span {
                line: start.line,
                col_start: start.span.0,
                col_end: self.toks[self.pos.saturating_sub(1)].span.1,
            };
            let _ = op_tok;
            left = Expr::new(ExprKind::Bin(op, Box::new(left), Box::new(right)), span);
        }
        Ok(left)
    }

    fn parse_or(&mut self) -> Result<Expr, ParseError> {
        self.binary_layer(&[(&TokKind::PipePipe, BinOp::LOr)], Self::parse_and)
    }
    fn parse_and(&mut self) -> Result<Expr, ParseError> {
        self.binary_layer(&[(&TokKind::AmpAmp, BinOp::LAnd)], Self::parse_eq)
    }
    fn parse_eq(&mut self) -> Result<Expr, ParseError> {
        self.binary_layer(
            &[
                (&TokKind::EqEq, BinOp::Eq),
                (&TokKind::NotEq, BinOp::Ne),
            ],
            Self::parse_rel,
        )
    }
    fn parse_rel(&mut self) -> Result<Expr, ParseError> {
        self.binary_layer(
            &[
                (&TokKind::Lt, BinOp::Lt),
                (&TokKind::Le, BinOp::Le),
                (&TokKind::Gt, BinOp::Gt),
                (&TokKind::Ge, BinOp::Ge),
            ],
            Self::parse_shift,
        )
    }
    fn parse_shift(&mut self) -> Result<Expr, ParseError> {
        self.binary_layer(
            &[
                (&TokKind::Shl, BinOp::Shl),
                (&TokKind::Shr, BinOp::Shr),
            ],
            Self::parse_add,
        )
    }
    fn parse_add(&mut self) -> Result<Expr, ParseError> {
        self.binary_layer(
            &[
                (&TokKind::Plus, BinOp::Add),
                (&TokKind::Minus, BinOp::Sub),
                (&TokKind::Pipe, BinOp::BitOr),
                (&TokKind::Caret, BinOp::BitXor),
            ],
            Self::parse_mul,
        )
    }
    fn parse_mul(&mut self) -> Result<Expr, ParseError> {
        self.binary_layer(
            &[
                (&TokKind::Star, BinOp::Mul),
                (&TokKind::Slash, BinOp::Div),
                (&TokKind::Percent, BinOp::Rem),
                (&TokKind::Amp, BinOp::BitAnd),
            ],
            Self::parse_unary,
        )
    }

    fn parse_unary(&mut self) -> Result<Expr, ParseError> {
        let start = self.tok().clone();
        let op = match self.peek() {
            TokKind::Minus => Some(UnOp::Neg),
            TokKind::Bang => Some(UnOp::Not),
            TokKind::Tilde => Some(UnOp::BitNot),
            _ => None,
        };
        if let Some(op) = op {
            self.bump();
            let inner = self.parse_unary()?;
            let span = Span {
                line: start.line,
                col_start: start.span.0,
                col_end: inner.span.col_end,
            };
            return Ok(Expr::new(ExprKind::Un(op, Box::new(inner)), span));
        }
        self.primary()
    }

    fn primary(&mut self) -> Result<Expr, ParseError> {
        let t = self.tok().clone();
        let kind = match t.kind.clone() {
            TokKind::Int(_, _) | TokKind::HexInt(_, _) => self.lit_from_digits(&t)?,
            TokKind::True => ExprKind::BoolLit(true),
            TokKind::False => ExprKind::BoolLit(false),
            TokKind::Ident(name) => ExprKind::Var(name),
            TokKind::LParen => {
                self.bump();
                let e = self.expr()?;
                let close = self.expect(&TokKind::RParen, "`)`")?;
                let span = Span {
                    line: t.line,
                    col_start: t.span.0,
                    col_end: close.span.1,
                };
                return Ok(Expr::new(e.kind, span));
            }
            k => {
                return Err(ParseError {
                    line: t.line,
                    col: t.span.0,
                    msg: format!("expected expression, found {}", tok_name(&k)),
                })
            }
        };
        self.bump();
        Ok(Expr::new(
            kind,
            Span {
                line: t.line,
                col_start: t.span.0,
                col_end: t.span.1,
            },
        ))
    }

    fn lit_from_digits(&mut self, t: &Token) -> Result<ExprKind, ParseError> {
        let hex = matches!(t.kind, TokKind::HexInt(..));
        let (digits, suffix) = match &t.kind {
            TokKind::Int(d, s) | TokKind::HexInt(d, s) => (d.clone(), s.clone()),
            _ => unreachable!(),
        };
        let suffix_ty = match suffix.as_deref() {
            None => None,
            Some("u8") => Some(Type::U8),
            Some("u16") => Some(Type::U16),
            Some("u32") => Some(Type::U32),
            Some("u64") => Some(Type::U64),
            Some(other) => {
                return Err(ParseError {
                    line: t.line,
                    col: t.span.0,
                    msg: format!("unknown suffix `{other}` (expected u8/u16/u32/u64)"),
                })
            }
        };
        let value = if hex {
            u64::from_str_radix(&digits, 16)
        } else {
            u64::from_str_radix(&digits, 10)
        }
        .map_err(|e| ParseError {
            line: t.line,
            col: t.span.0,
            msg: format!("integer literal out of u64 range: {e}"),
        })?;
        if let Some(ty) = suffix_ty {
            if value & !ty.mask() != 0 {
                return Err(ParseError {
                    line: t.line,
                    col: t.span.0,
                    msg: format!("literal {value} does not fit in {}", ty.name()),
                });
            }
        }
        Ok(ExprKind::Lit(Lit {
            value,
            suffix: suffix_ty,
            radix: if hex { Radix::Hex } else { Radix::Dec },
        }))
    }
}

fn span_of(a: &Token, b: &Token) -> Span {
    Span {
        line: a.line,
        col_start: a.span.0,
        col_end: b.span.1.max(a.span.0),
    }
}

fn tok_name(k: &TokKind) -> String {
    match k {
        TokKind::Ident(s) => format!("identifier `{s}`"),
        TokKind::Int(..) => "integer".into(),
        TokKind::Eof => "end of input".into(),
        TokKind::Semi => "`;`".into(),
        TokKind::Comma => "`,`".into(),
        TokKind::Colon => "`:`".into(),
        TokKind::LBrace => "`{`".into(),
        TokKind::RBrace => "`}`".into(),
        TokKind::LParen => "`(`".into(),
        TokKind::RParen => "`)`".into(),
        TokKind::Assign => "`=`".into(),
        other => format!("{other:?}"),
    }
}

/// Lex and parse source text; ids are still placeholders at this stage.
pub fn parse_program(src: &str) -> Result<Program, ParseError> {
    let toks = lex(src).map_err(|e| ParseError {
        line: e.line,
        col: e.col,
        msg: e.msg,
    })?;
    let mut p = Parser { toks, pos: 0 }.program()?;
    number_program(&mut p);
    Ok(p)
}
