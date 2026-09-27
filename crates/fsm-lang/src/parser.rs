//! Recursive-descent parser for the textual DSL and standalone expressions.
//!
//! System syntax (EBNF, keywords lowercase):
//!
//! ```text
//! system    := "system" IDENT? "{" section+ "}"
//! section   := var_section | init_section | terminal_section | trans_section
//! var_section    := "var" "{" vardecl (";" vardecl)* ";"? "}"
//! vardecl        := IDENT ":" "bool"
//!                | IDENT ":" "int" "[" INT ".." INT "]"
//!                | IDENT ":" "enum" "{" IDENT ("," IDENT)* "}"
//! init_section   := "init" "{" expr "}"
//!                | "init" "{" IDENT ":=" expr ("," IDENT ":=" expr)* "}"
//! terminal_section := "terminal" "{" expr "}"
//! trans_section  := "transition" IDENT "{"
//!                      "guard" ":" expr ";"
//!                      "then"  ":" assign ("," assign)* ";"?
//!                  "}"
//! assign         := IDENT ":=" expr
//! ```
//!
//! Expressions: booleans/integers, `( )`, unary `- !`/`not`, `* / mod`,
//! `+ -`, `< <= > >=`, `== !=`, `&&`, `||`, and `if c then a else b`.

use crate::ast::*;
use crate::error::{BuildError, BuildErrorKind};
use crate::lexer::Lexer;
use crate::system::Domain;
use crate::token::{Token, TokenKind};

pub fn parse_system(src: &str) -> Result<RawSystem, BuildError> {
    let tokens = Lexer::new(src).tokenize()?;
    let mut p = Parser {
        tokens,
        pos: 0,
        src,
    };
    let raw = p.parse_system_inner()?;
    Ok(raw)
}

/// Parse a standalone expression (used by property strings and JSON).
pub fn parse_expr(src: &str) -> Result<Expr, BuildError> {
    let tokens = Lexer::new(src).tokenize()?;
    let mut p = Parser {
        tokens,
        pos: 0,
        src,
    };
    let e = p.parse_expr()?;
    p.expect(TokenKind::Eof, "end of expression")?;
    Ok(e)
}

struct Parser<'a> {
    tokens: Vec<Token>,
    pos: usize,
    src: &'a str,
}

impl<'a> Parser<'a> {
    fn cur(&self) -> &TokenKind {
        &self.tokens[self.pos].kind
    }

    fn cur_tok(&self) -> &Token {
        &self.tokens[self.pos]
    }

    fn at(&self, k: &TokenKind) -> bool {
        self.cur() == k
    }

    fn bump(&mut self) -> Token {
        let t = self.tokens[self.pos].clone();
        if !matches!(t.kind, TokenKind::Eof) {
            self.pos += 1;
        }
        t
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

    fn err(&self, kind: BuildErrorKind, msg: impl Into<String>) -> BuildError {
        let tok = self.cur_tok();
        BuildError::new(kind, msg).with_source_pos(tok.pos, self.line_col(tok.pos))
    }

    fn expect(&mut self, k: TokenKind, what: &str) -> Result<Token, BuildError> {
        if self.at(&k) {
            Ok(self.bump())
        } else {
            Err(self.err(
                BuildErrorKind::Parse,
                format!("expected {what}, found {}", describe(&k)),
            ))
        }
    }

    fn expect_ident(&mut self) -> Result<String, BuildError> {
        match self.cur().clone() {
            TokenKind::Ident(s) => {
                self.bump();
                Ok(s)
            }
            other => Err(self.err(
                BuildErrorKind::Parse,
                format!("expected identifier, found {other:?}"),
            )),
        }
    }

    fn parse_system_inner(&mut self) -> Result<RawSystem, BuildError> {
        let mut name = None;
        if self.at(&TokenKind::KwSystem) {
            self.bump();
            if let TokenKind::Ident(s) = self.cur().clone() {
                self.bump();
                name = Some(s);
            }
        }
        self.expect(TokenKind::LBrace, "'{' opening the system body")?;

        let mut vars: Vec<(String, Domain)> = Vec::new();
        let mut init_predicate: Option<Expr> = None;
        let mut init_state: Vec<(String, Expr)> = Vec::new();
        let mut transitions: Vec<RawTransition> = Vec::new();
        let mut terminals: Vec<Expr> = Vec::new();

        while !self.at(&TokenKind::RBrace) && !self.at(&TokenKind::Eof) {
            match self.cur().clone() {
                TokenKind::KwVar => {
                    self.bump();
                    self.expect(TokenKind::LBrace, "'{' after 'var'")?;
                    while !self.at(&TokenKind::RBrace) {
                        vars.push(self.parse_vardecl()?);
                        if self.at(&TokenKind::Semicolon) {
                            self.bump();
                        } else if !self.at(&TokenKind::RBrace) {
                            return Err(self.err(
                                BuildErrorKind::Parse,
                                "expected ';' between variable declarations",
                            ));
                        }
                    }
                    self.expect(TokenKind::RBrace, "'}' closing the var section")?;
                }
                TokenKind::KwInit => {
                    self.bump();
                    self.expect(TokenKind::LBrace, "'{' after 'init'")?;
                    // Distinguish concrete initial state (`x := 0`) from a
                    // predicate (`x == 0`) with one token of lookahead.
                    if matches!(self.cur(), TokenKind::Ident(_))
                        && self.tokens.get(self.pos + 1).map(|t| &t.kind)
                            == Some(&TokenKind::Assign)
                    {
                        while !self.at(&TokenKind::RBrace) {
                            let v = self.expect_ident()?;
                            self.expect(TokenKind::Assign, "':=' in initial state")?;
                            let e = self.parse_expr()?;
                            init_state.push((v, e));
                            if self.at(&TokenKind::Comma) {
                                self.bump();
                            } else if !self.at(&TokenKind::RBrace) {
                                return Err(self.err(
                                    BuildErrorKind::Parse,
                                    "expected ',' between initial-state assignments",
                                ));
                            }
                        }
                    } else {
                        let e = self.parse_expr()?;
                        init_predicate = Some(e);
                    }
                    self.expect(TokenKind::RBrace, "'}' closing the init section")?;
                }
                TokenKind::KwTerminal => {
                    self.bump();
                    self.expect(TokenKind::LBrace, "'{' after 'terminal'")?;
                    let e = self.parse_expr()?;
                    terminals.push(e);
                    self.expect(TokenKind::RBrace, "'}' closing the terminal section")?;
                }
                TokenKind::KwTransition => {
                    transitions.push(self.parse_transition()?);
                }
                other => {
                    return Err(self.err(
                        BuildErrorKind::Parse,
                        format!(
                            "expected 'var', 'init', 'terminal' or 'transition', found {other:?}"
                        ),
                    ));
                }
            }
        }
        self.expect(TokenKind::RBrace, "'}' closing the system body")?;
        self.expect(TokenKind::Eof, "end of system")?;

        Ok(RawSystem {
            name,
            vars,
            init_predicate,
            init_state,
            transitions,
            terminals,
        })
    }

    fn parse_vardecl(&mut self) -> Result<(String, Domain), BuildError> {
        let v = self.expect_ident()?;
        self.expect(TokenKind::Colon, "':' after variable name")?;
        let domain =
            match self.cur().clone() {
                TokenKind::KwBool => {
                    self.bump();
                    Domain::Bool
                }
                TokenKind::KwInt => {
                    self.bump();
                    self.expect(TokenKind::LBracket, "'[' before integer range")?;
                    let lo = self.parse_signed_bound()?;
                    self.expect(TokenKind::DotDot, "'..' in integer range")?;
                    let hi = self.parse_signed_bound()?;
                    self.expect(TokenKind::RBracket, "']' after integer range")?;
                    Domain::IntRange { lo, hi }
                }
                TokenKind::KwEnum => {
                    self.bump();
                    self.expect(TokenKind::LBrace, "'{' after 'enum'")?;
                    let mut variants = Vec::new();
                    while !self.at(&TokenKind::RBrace) {
                        variants.push(self.expect_ident()?);
                        if self.at(&TokenKind::Comma) {
                            self.bump();
                        } else if !self.at(&TokenKind::RBrace) {
                            return Err(self
                                .err(BuildErrorKind::Parse, "expected ',' between enum variants"));
                        }
                    }
                    self.expect(TokenKind::RBrace, "'}' closing enum variants")?;
                    Domain::Enum { variants }
                }
                other => return Err(self.err(
                    BuildErrorKind::Parse,
                    format!(
                        "variable type must be bool, int[lo..hi] or enum {{...}}, found {other:?}"
                    ),
                )),
            };
        Ok((v, domain))
    }

    fn parse_signed_bound(&mut self) -> Result<i64, BuildError> {
        let neg = if self.at(&TokenKind::Minus) {
            self.bump();
            true
        } else {
            false
        };
        match self.bump().kind {
            TokenKind::Int(i) => Ok(if neg {
                i.checked_neg().ok_or_else(|| {
                    self.err(BuildErrorKind::InvalidDomain, "range bound overflows i64")
                })?
            } else {
                i
            }),
            _ => Err(self.err(
                BuildErrorKind::Parse,
                "expected integer bound in int[lo..hi]",
            )),
        }
    }

    fn parse_transition(&mut self) -> Result<RawTransition, BuildError> {
        self.expect(TokenKind::KwTransition, "'transition'")?;
        let name = self.expect_ident()?;
        self.expect(TokenKind::LBrace, "'{' opening transition body")?;
        self.expect(TokenKind::KwGuard, "'guard'")?;
        self.expect(TokenKind::Colon, "':' after guard")?;
        let guard = self.parse_expr()?;
        self.expect(TokenKind::Semicolon, "';' after guard expression")?;
        self.expect(TokenKind::KwThen, "'then'")?;
        self.expect(TokenKind::Colon, "':' after then")?;
        let mut assign = Vec::new();
        while !self.at(&TokenKind::RBrace) {
            let target = self.expect_ident()?;
            self.expect(TokenKind::Assign, "':=' in assignment")?;
            let rhs = self.parse_expr()?;
            assign.push(Assign {
                target,
                target_index: None,
                rhs,
            });
            if matches!(self.cur(), TokenKind::Comma | TokenKind::Semicolon) {
                self.bump();
            } else if !self.at(&TokenKind::RBrace) {
                return Err(self.err(BuildErrorKind::Parse, "expected ',' between assignments"));
            }
        }
        self.expect(TokenKind::RBrace, "'}' closing transition body")?;
        Ok(RawTransition {
            name,
            guard,
            assign,
        })
    }

    // ---- expressions ----------------------------------------------------

    fn parse_expr(&mut self) -> Result<Expr, BuildError> {
        self.parse_or()
    }

    fn parse_or(&mut self) -> Result<Expr, BuildError> {
        let mut l = self.parse_and()?;
        while self.at(&TokenKind::Or) {
            self.bump();
            let r = self.parse_and()?;
            l = Expr::Binary {
                op: BinOp::Or,
                l: Box::new(l),
                r: Box::new(r),
            };
        }
        Ok(l)
    }

    fn parse_and(&mut self) -> Result<Expr, BuildError> {
        let mut l = self.parse_cmp()?;
        while self.at(&TokenKind::And) {
            self.bump();
            let r = self.parse_cmp()?;
            l = Expr::Binary {
                op: BinOp::And,
                l: Box::new(l),
                r: Box::new(r),
            };
        }
        Ok(l)
    }

    fn parse_cmp(&mut self) -> Result<Expr, BuildError> {
        let l = self.parse_add()?;
        let op = match self.cur() {
            TokenKind::Lt => BinOp::Lt,
            TokenKind::Le => BinOp::Le,
            TokenKind::Gt => BinOp::Gt,
            TokenKind::Ge => BinOp::Ge,
            TokenKind::EqEq => BinOp::Eq,
            TokenKind::Neq => BinOp::Ne,
            _ => return Ok(l),
        };
        self.bump();
        let r = self.parse_add()?;
        Ok(Expr::Binary {
            op,
            l: Box::new(l),
            r: Box::new(r),
        })
    }

    fn parse_add(&mut self) -> Result<Expr, BuildError> {
        let mut l = self.parse_mul()?;
        loop {
            let op = match self.cur() {
                TokenKind::Plus => BinOp::Add,
                TokenKind::Minus => BinOp::Sub,
                _ => break,
            };
            self.bump();
            let r = self.parse_mul()?;
            l = Expr::Binary {
                op,
                l: Box::new(l),
                r: Box::new(r),
            };
        }
        Ok(l)
    }

    fn parse_mul(&mut self) -> Result<Expr, BuildError> {
        let mut l = self.parse_unary()?;
        loop {
            let op = match self.cur() {
                TokenKind::Star => BinOp::Mul,
                TokenKind::Slash => BinOp::Div,
                TokenKind::KwMod => BinOp::Mod,
                _ => break,
            };
            self.bump();
            let r = self.parse_unary()?;
            l = Expr::Binary {
                op,
                l: Box::new(l),
                r: Box::new(r),
            };
        }
        Ok(l)
    }

    fn parse_unary(&mut self) -> Result<Expr, BuildError> {
        match self.cur() {
            TokenKind::Bang => {
                self.bump();
                let e = self.parse_unary()?;
                Ok(Expr::Unary {
                    op: UnOp::Not,
                    e: Box::new(e),
                })
            }
            TokenKind::Minus => {
                self.bump();
                let e = self.parse_unary()?;
                Ok(Expr::Unary {
                    op: UnOp::Neg,
                    e: Box::new(e),
                })
            }
            _ => self.parse_primary(),
        }
    }

    fn parse_primary(&mut self) -> Result<Expr, BuildError> {
        let tok = self.cur_tok().clone();
        match tok.kind {
            TokenKind::Int(i) => {
                self.bump();
                Ok(Expr::Int(i))
            }
            TokenKind::KwTrue => {
                self.bump();
                Ok(Expr::Bool(true))
            }
            TokenKind::KwFalse => {
                self.bump();
                Ok(Expr::Bool(false))
            }
            TokenKind::Ident(name) => {
                self.bump();
                Ok(Expr::Name { name, res: None })
            }
            TokenKind::LParen => {
                self.bump();
                let e = self.parse_expr()?;
                self.expect(TokenKind::RParen, "')'")?;
                Ok(e)
            }
            TokenKind::KwIf => {
                self.bump();
                let cond = self.parse_expr()?;
                self.expect(TokenKind::KwThen, "'then'")?;
                let thn = self.parse_expr()?;
                self.expect(TokenKind::KwElse, "'else'")?;
                let els = self.parse_expr()?;
                Ok(Expr::Ite {
                    cond: Box::new(cond),
                    thn: Box::new(thn),
                    els: Box::new(els),
                })
            }
            other => Err(self.err(
                BuildErrorKind::Parse,
                format!("expected expression, found {other:?}"),
            )),
        }
    }
}

fn describe(k: &TokenKind) -> String {
    format!("{k:?}")
}
