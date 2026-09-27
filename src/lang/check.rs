//! Semantic validation and type inference.
//!
//! Rules: every arithmetic expression has exactly one fixed unsigned width;
//! widths never implicitly widen; shifts by an amount >= the width
//! yield 0, matching SMT-LIB bvshl/bvlshr. An unsuffixed integer literal inherits its width from context;
//! if no context fixes it, that is a compile error rather than a guess.

use std::collections::HashSet;

use super::ast::*;
use super::types::Type;

#[derive(Debug)]
pub struct CheckError {
    pub node_id: Option<NodeId>,
    pub line: u32,
    pub col: u32,
    pub msg: String,
}

impl std::fmt::Display for CheckError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "line {} col {}: {}", self.line, self.col, self.msg)
    }
}
impl std::error::Error for CheckError {}

fn err_at(id: NodeId, line: u32, col: u32, msg: String) -> CheckError {
    CheckError {
        node_id: Some(id),
        line,
        col,
        msg,
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Kind {
    Int(Type),
    Bool,
}

struct Checker {
    scopes: Vec<HashSet<String>>,
    types: std::collections::HashMap<String, Type>,
}

impl Checker {
    fn new(params: &[Param]) -> Self {
        let mut root = HashSet::new();
        let mut types = std::collections::HashMap::new();
        for p in params {
            root.insert(p.name.clone());
            types.insert(p.name.clone(), p.ty);
        }
        Checker {
            scopes: vec![root],
            types,
        }
    }

    fn declared(&self, name: &str) -> bool {
        self.scopes.iter().any(|h| h.contains(name))
    }
    fn push(&mut self) {
        self.scopes.push(HashSet::new());
    }
    fn pop(&mut self) {
        self.scopes.pop();
    }

    fn block(&mut self, stmts: &mut [Stmt]) -> Result<(), CheckError> {
        self.push();
        for s in stmts.iter_mut() {
            self.stmt(s)?;
        }
        self.pop();
        Ok(())
    }

    fn stmt(&mut self, s: &mut Stmt) -> Result<(), CheckError> {
        let sid = s.id;
        let (sline, scol) = (s.span.line, s.span.col_start);
        match &mut s.kind {
            StmtKind::Let { name, ty, value } => {
                if self.declared(name) {
                    return Err(err_at(sid, sline, scol, format!("`{name}` is already declared")));
                }
                let ty = *ty;
                match self.infer(value, Some(ty))? {
                    Kind::Int(t) if t == ty => {}
                    Kind::Int(t) => {
                        return Err(err_at(
                            sid,
                            sline,
                            scol,
                            format!("let `{name}`: declared {}, inferred {}", ty.name(), t.name()),
                        ));
                    }
                    Kind::Bool => {
                        return Err(err_at(
                            sid,
                            sline,
                            scol,
                            format!("let `{name}` declared as {} but initializer is boolean", ty.name()),
                        ));
                    }
                }
                self.scopes.last_mut().unwrap().insert(name.clone());
                self.types.insert(name.clone(), ty);
            }
            StmtKind::Assign { name, value } => {
                let ty = self
                    .types
                    .get(name)
                    .copied()
                    .ok_or_else(|| err_at(sid, sline, scol, format!("assignment to undeclared `{name}`")))?;
                if self.infer(value, Some(ty))? != Kind::Int(ty) {
                    return Err(err_at(
                        sid,
                        sline,
                        scol,
                        format!("cannot assign to `{name}` ({}) from a different type", ty.name()),
                    ));
                }
            }
            StmtKind::Assert { cond, .. } | StmtKind::Assume { cond } => {
                if self.infer(cond, None)? != Kind::Bool {
                    let (id, l, c) = (cond.id, cond.span.line, cond.span.col_start);
                    return Err(err_at(id, l, c, "assert/assume condition must be boolean".into()));
                }
            }
            StmtKind::If { cond, then, els } => {
                if self.infer(cond, None)? != Kind::Bool {
                    let (id, l, c) = (cond.id, cond.span.line, cond.span.col_start);
                    return Err(err_at(id, l, c, "if condition must be boolean".into()));
                }
                self.block(then)?;
                self.block(els)?;
            }
            StmtKind::While { cond, body } => {
                if self.infer(cond, None)? != Kind::Bool {
                    let (id, l, c) = (cond.id, cond.span.line, cond.span.col_start);
                    return Err(err_at(id, l, c, "while condition must be boolean".into()));
                }
                self.block(body)?;
            }
        }
        Ok(())
    }

    fn infer(&mut self, e: &mut Expr, expected: Option<Type>) -> Result<Kind, CheckError> {
        let (eid, eline, ecol) = (e.id, e.span.line, e.span.col_start);
        let kind = match &mut e.kind {
            ExprKind::Lit(lit) => {
                if let Some(t) = lit.suffix {
                    Kind::Int(t)
                } else {
                    let t = expected.ok_or_else(|| {
                        err_at(
                            eid,
                            eline,
                            ecol,
                            format!("cannot infer width of literal {}; add a suffix like {}u8", lit.value, lit.value),
                        )
                    })?;
                    if lit.value & !t.mask() != 0 {
                        return Err(err_at(
                            eid,
                            eline,
                            ecol,
                            format!("literal {} does not fit in {}", lit.value, t.name()),
                        ));
                    }
                    Kind::Int(t)
                }
            }
            ExprKind::BoolLit(_) => Kind::Bool,
            ExprKind::Var(name) => match self.types.get(name).copied() {
                Some(t) => Kind::Int(t),
                None => return Err(err_at(eid, eline, ecol, format!("undeclared variable `{name}`"))),
            },
            ExprKind::Un(op, inner) => match op {
                UnOp::Not => {
                    if self.infer(inner, None)? != Kind::Bool {
                        let (id, l, c) = (inner.id, inner.span.line, inner.span.col_start);
                        return Err(err_at(id, l, c, "`!` requires a boolean operand".into()));
                    }
                    Kind::Bool
                }
                UnOp::Neg | UnOp::BitNot => match self.infer(inner, expected)? {
                    k @ Kind::Int(_) => k,
                    Kind::Bool => {
                        let (id, l, c) = (inner.id, inner.span.line, inner.span.col_start);
                        return Err(err_at(id, l, c, "`-`/`~` requires an integer operand".into()));
                    }
                },
            },
            ExprKind::Bin(op, a, b) => self.infer_binary(eid, eline, ecol, *op, a, b, expected)?,
        };
        if let Kind::Int(t) = kind {
            e.ty = Some(t);
        }
        Ok(kind)
    }

    fn infer_binary(
        &mut self,
        eid: NodeId,
        eline: u32,
        ecol: u32,
        op: BinOp,
        a: &mut Expr,
        b: &mut Expr,
        expected: Option<Type>,
    ) -> Result<Kind, CheckError> {
        match op {
            BinOp::LAnd | BinOp::LOr => {
                if self.infer(a, None)? != Kind::Bool || self.infer(b, None)? != Kind::Bool {
                    return Err(err_at(eid, eline, ecol, format!("`{}` requires boolean operands", op.symbol())));
                }
                Ok(Kind::Bool)
            }
            BinOp::Eq | BinOp::Ne => {
                let ka = self.infer(a, None)?;
                let kb = self.infer(
                    b,
                    match ka {
                        Kind::Int(t) => Some(t),
                        Kind::Bool => None,
                    },
                )?;
                if ka != kb {
                    return Err(err_at(
                        eid,
                        eline,
                        ecol,
                        format!("`{}` operands must match: {ka:?} vs {kb:?}", op.symbol()),
                    ));
                }
                Ok(Kind::Bool)
            }
            _ => {
                // Integer binary operators. Left side is inferred first and
                // fixes the width of an unsuffixed literal on the right; if
                // the left side is itself ambiguous, the outer expectation
                // breaks the tie.
                let ka = self.infer(a, expected)?;
                let hint = match ka {
                    Kind::Int(t) => Some(t),
                    Kind::Bool => expected,
                };
                let kb = self.infer(b, hint)?;
                let (ta, tb) = match (ka, kb) {
                    (Kind::Int(x), Kind::Int(y)) => (x, y),
                    _ => {
                        return Err(err_at(eid, eline, ecol, format!("`{}` requires integer operands", op.symbol())))
                    }
                };
                if ta != tb {
                    return Err(err_at(
                        eid,
                        eline,
                        ecol,
                        format!("`{}` mixes {} and {} (no implicit conversion)", op.symbol(), ta.name(), tb.name()),
                    ));
                }
                if op.is_boolean() {
                    Ok(Kind::Bool)
                } else {
                    Ok(Kind::Int(ta))
                }
            }
        }
    }
}

/// Validate the program and fill in inferred widths in place.
pub fn check_program(p: &mut Program) -> Result<(), CheckError> {
    let mut seen = HashSet::new();
    for par in &p.params {
        if !seen.insert(par.name.clone()) {
            return Err(CheckError {
                node_id: None,
                line: 0,
                col: 0,
                msg: format!("duplicate parameter `{}`", par.name),
            });
        }
    }
    let mut chk = Checker::new(&p.params);
    for s in p.body.iter_mut() {
        chk.stmt(s)?;
    }
    Ok(())
}
