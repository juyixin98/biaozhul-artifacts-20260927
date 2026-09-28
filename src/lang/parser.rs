use super::ast::*;
use super::error::LangError;
use super::lexer::{lex, Tok, Token};

pub fn parse(src: &str) -> Result<Vec<Stmt>, LangError> {
    let tokens = lex(src)?;
    let mut p = Parser { tokens, pos: 0 };
    p.program()
}

struct Parser {
    tokens: Vec<Token>,
    pos: usize,
}

impl Parser {
    fn peek(&self) -> &Tok {
        &self.tokens[self.pos].tok
    }

    fn cur_span(&self) -> Span {
        self.tokens[self.pos].span
    }

    fn at(&self, t: &Tok) -> bool {
        std::mem::discriminant(self.peek()) == std::mem::discriminant(t)
    }

    fn bump(&mut self) -> Token {
        let tok = self.tokens[self.pos].clone();
        if !matches!(tok.tok, Tok::Eof) {
            self.pos += 1;
        }
        tok
    }

    fn expect(&mut self, t: Tok, what: &str) -> Result<Token, LangError> {
        if self.at(&t) {
            Ok(self.bump())
        } else {
            Err(LangError::new(
                format!("expected {what}, found `{}`", tok_name(self.peek())),
                self.cur_span(),
            ))
        }
    }

    fn expect_ident(&mut self) -> Result<(String, Span), LangError> {
        match self.peek().clone() {
            Tok::Ident(name) => {
                let t = self.bump();
                Ok((name, t.span))
            }
            other => Err(LangError::new(
                format!("expected identifier, found `{}`", tok_name(&other)),
                self.cur_span(),
            )),
        }
    }

    fn signed_int(&mut self) -> Result<(i64, Span), LangError> {
        let (neg, sign_span) = if self.at(&Tok::Minus) {
            let t = self.bump();
            (true, t.span)
        } else {
            (false, self.cur_span())
        };
        match self.peek().clone() {
            Tok::Int(v) => {
                let t = self.bump();
                let value = if neg {
                    v.checked_neg().ok_or_else(|| {
                        LangError::new("integer literal is outside the i64 range", sign_span)
                    })?
                } else {
                    v
                };
                Ok((value, t.span))
            }
            other => Err(LangError::new(
                format!("expected integer literal, found `{}`", tok_name(&other)),
                self.cur_span(),
            )),
        }
    }

    fn program(&mut self) -> Result<Vec<Stmt>, LangError> {
        let mut stmts = Vec::new();
        while !self.at(&Tok::Eof) {
            stmts.push(self.stmt()?);
        }
        Ok(stmts)
    }

    fn block(&mut self) -> Result<(Vec<Stmt>, Span), LangError> {
        let lb = self.expect(Tok::LBrace, "`{`")?;
        let mut stmts = Vec::new();
        while !self.at(&Tok::RBrace) && !self.at(&Tok::Eof) {
            stmts.push(self.stmt()?);
        }
        self.expect(Tok::RBrace, "`}`")?;
        Ok((stmts, lb.span))
    }

    fn stmt(&mut self) -> Result<Stmt, LangError> {
        match self.peek() {
            Tok::Let => self.input_decl(),
            Tok::Array => self.array_decl(),
            Tok::If => self.if_stmt(),
            Tok::While => self.while_stmt(),
            Tok::Assert => self.assert_stmt(),
            Tok::Ident(_) => self.assign_like(),
            other => Err(LangError::new(
                format!("expected a statement, found `{}`", tok_name(other)),
                self.cur_span(),
            )),
        }
    }

    fn input_decl(&mut self) -> Result<Stmt, LangError> {
        let kw = self.bump(); // let
        let (name, _) = self.expect_ident()?;
        self.expect(Tok::Colon, "`:`")?;
        self.expect(Tok::LBracket, "`[`")?;
        let (lo, _) = self.signed_int()?;
        self.expect(Tok::Comma, "`,`")?;
        let (hi, _) = self.signed_int()?;
        self.expect(Tok::RBracket, "`]`")?;
        self.expect(Tok::Semicolon, "`;`")?;
        if lo > hi {
            return Err(LangError::new(
                format!("empty input range [{lo}, {hi}] in `let {name}`"),
                kw.span,
            ));
        }
        Ok(Stmt::Input {
            name,
            lo,
            hi,
            span: kw.span,
        })
    }

    fn array_decl(&mut self) -> Result<Stmt, LangError> {
        let kw = self.bump(); // array
        let (name, _) = self.expect_ident()?;
        self.expect(Tok::LBracket, "`[`")?;
        let (len_raw, len_span) = self.signed_int()?;
        self.expect(Tok::RBracket, "`]`")?;
        self.expect(Tok::Semicolon, "`;`")?;
        if len_raw <= 0 {
            return Err(LangError::new(
                "array length must be a positive i64 constant",
                len_span,
            ));
        }
        // length bounded so concrete enumeration / memory stays small
        if len_raw > 1_048_576 {
            return Err(LangError::new("array length exceeds 1_048_576", len_span));
        }
        Ok(Stmt::ArrayDecl {
            name,
            len: len_raw as usize,
            span: kw.span,
        })
    }

    fn if_stmt(&mut self) -> Result<Stmt, LangError> {
        let kw = self.bump(); // if
        let cond = self.or_cond()?;
        let (then_body, _) = self.block()?;
        let else_body = if self.at(&Tok::Else) {
            self.bump();
            if self.at(&Tok::If) {
                // else if ...
                vec![self.if_stmt()?]
            } else {
                let (body, _) = self.block()?;
                body
            }
        } else {
            Vec::new()
        };
        Ok(Stmt::If {
            cond,
            then_body,
            else_body,
            span: kw.span,
        })
    }

    fn while_stmt(&mut self) -> Result<Stmt, LangError> {
        let kw = self.bump(); // while
        let cond = self.or_cond()?;
        let (body, _) = self.block()?;
        Ok(Stmt::While {
            cond,
            body,
            span: kw.span,
        })
    }

    fn assert_stmt(&mut self) -> Result<Stmt, LangError> {
        let kw = self.bump(); // assert
        let cond = self.or_cond()?;
        self.expect(Tok::Semicolon, "`;`")?;
        Ok(Stmt::Assert {
            cond,
            span: kw.span,
        })
    }

    fn assign_like(&mut self) -> Result<Stmt, LangError> {
        let (name, name_span) = self.expect_ident()?;
        if self.at(&Tok::LBracket) {
            self.bump();
            let index = self.expr()?;
            self.expect(Tok::RBracket, "`]`")?;
            self.expect(Tok::Assign, "`:=`")?;
            let value = self.expr()?;
            self.expect(Tok::Semicolon, "`;`")?;
            Ok(Stmt::ArrayStore {
                name,
                index,
                value,
                span: name_span,
            })
        } else {
            self.expect(Tok::Assign, "`:=`")?;
            let expr = self.expr()?;
            self.expect(Tok::Semicolon, "`;`")?;
            Ok(Stmt::Assign {
                name,
                expr,
                span: name_span,
            })
        }
    }

    // ---- conditions ----

    fn or_cond(&mut self) -> Result<Cond, LangError> {
        let mut lhs = self.and_cond()?;
        while self.at(&Tok::OrOr) {
            let op = self.bump();
            let rhs = self.and_cond()?;
            lhs = Cond::Or(Box::new(lhs), Box::new(rhs), op.span);
        }
        Ok(lhs)
    }

    fn and_cond(&mut self) -> Result<Cond, LangError> {
        let mut lhs = self.not_cond()?;
        while self.at(&Tok::AndAnd) {
            let op = self.bump();
            let rhs = self.not_cond()?;
            lhs = Cond::And(Box::new(lhs), Box::new(rhs), op.span);
        }
        Ok(lhs)
    }

    fn not_cond(&mut self) -> Result<Cond, LangError> {
        if self.at(&Tok::Bang) {
            let bang = self.bump();
            let inner = self.not_cond()?;
            return Ok(Cond::Not(Box::new(inner), bang.span));
        }
        if self.at(&Tok::LParen) {
            self.bump();
            let inner = self.or_cond()?;
            self.expect(Tok::RParen, "`)`")?;
            return Ok(inner);
        }
        self.cmp_cond()
    }

    fn cmp_cond(&mut self) -> Result<Cond, LangError> {
        let lhs = self.expr()?;
        let (op, span) = match self.peek() {
            Tok::Lt => (CmpOp::Lt, self.bump().span),
            Tok::Le => (CmpOp::Le, self.bump().span),
            Tok::EqEq => (CmpOp::Eq, self.bump().span),
            Tok::Ne => (CmpOp::Ne, self.bump().span),
            Tok::Ge => (CmpOp::Ge, self.bump().span),
            Tok::Gt => (CmpOp::Gt, self.bump().span),
            other => {
                return Err(LangError::new(
                    format!(
                        "expected a comparison operator after expression, found `{}`",
                        tok_name(other)
                    ),
                    self.cur_span(),
                ));
            }
        };
        let rhs = self.expr()?;
        Ok(Cond::Cmp { op, lhs, rhs, span })
    }

    // ---- expressions ----

    fn expr(&mut self) -> Result<Expr, LangError> {
        let mut lhs = self.term()?;
        loop {
            match self.peek() {
                Tok::Plus => {
                    let op = self.bump();
                    let rhs = self.term()?;
                    lhs = Expr::Add(Box::new(lhs), Box::new(rhs), op.span);
                }
                Tok::Minus => {
                    let op = self.bump();
                    let rhs = self.term()?;
                    lhs = Expr::Sub(Box::new(lhs), Box::new(rhs), op.span);
                }
                _ => break,
            }
        }
        Ok(lhs)
    }

    fn term(&mut self) -> Result<Expr, LangError> {
        let mut lhs = self.factor()?;
        while self.at(&Tok::Star) {
            let op = self.bump();
            let rhs = self.factor()?;
            lhs = Expr::Mul(Box::new(lhs), Box::new(rhs), op.span);
        }
        Ok(lhs)
    }

    fn factor(&mut self) -> Result<Expr, LangError> {
        match self.peek().clone() {
            Tok::Minus => {
                let op = self.bump();
                let inner = self.factor()?;
                Ok(Expr::Neg(Box::new(inner), op.span))
            }
            Tok::Int(v) => {
                let t = self.bump();
                Ok(Expr::Int(v, t.span))
            }
            Tok::Ident(name) => {
                let t = self.bump();
                if self.at(&Tok::LBracket) {
                    self.bump();
                    let index = self.expr()?;
                    let rb = self.expect(Tok::RBracket, "`]`")?;
                    let span = Span::new(
                        t.span.line,
                        t.span.col,
                        t.span.offset,
                        rb.span.offset + rb.span.len - t.span.offset,
                    );
                    Ok(Expr::Load {
                        array: name,
                        index: Box::new(index),
                        span,
                    })
                } else {
                    Ok(Expr::Var(name, t.span))
                }
            }
            Tok::LParen => {
                self.bump();
                let inner = self.expr()?;
                self.expect(Tok::RParen, "`)`")?;
                Ok(inner)
            }
            other => Err(LangError::new(
                format!("expected an expression, found `{}`", tok_name(&other)),
                self.cur_span(),
            )),
        }
    }
}

fn tok_name(t: &Tok) -> String {
    match t {
        Tok::Ident(s) => format!("identifier `{s}`"),
        Tok::Int(v) => format!("integer `{v}`"),
        Tok::Let => "`let`".into(),
        Tok::Array => "`array`".into(),
        Tok::If => "`if`".into(),
        Tok::Else => "`else`".into(),
        Tok::While => "`while`".into(),
        Tok::Assert => "`assert`".into(),
        Tok::Assign => "`:=`".into(),
        Tok::LBracket => "`[`".into(),
        Tok::RBracket => "`]`".into(),
        Tok::LBrace => "`{`".into(),
        Tok::RBrace => "`}`".into(),
        Tok::LParen => "`(`".into(),
        Tok::RParen => "`)`".into(),
        Tok::Semicolon => "`;`".into(),
        Tok::Colon => "`:`".into(),
        Tok::Comma => "`,`".into(),
        Tok::Plus => "`+`".into(),
        Tok::Minus => "`-`".into(),
        Tok::Star => "`*`".into(),
        Tok::Bang => "`!`".into(),
        Tok::Lt => "`<`".into(),
        Tok::Le => "`<=`".into(),
        Tok::EqEq => "`==`".into(),
        Tok::Ne => "`!=`".into(),
        Tok::Ge => "`>=`".into(),
        Tok::Gt => "`>`".into(),
        Tok::AndAnd => "`&&`".into(),
        Tok::OrOr => "`||`".into(),
        Tok::Eof => "end of input".into(),
    }
}
