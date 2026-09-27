//! Name resolution and static validation:
//! duplicate declarations, undeclared names, array/scalar confusion,
//! immutable `const`, and array length limits.
use crate::ast::*;
use crate::span::Span;
use std::collections::HashMap;

/// Maximum statically-sized array (1 Mi elements). Keeps concrete enumeration
/// and abstract cell maps bounded for review.
pub const MAX_ARRAY_LEN: u64 = 1 << 20;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ValidationError {
    pub message: String,
    pub span: Span,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeclKind {
    Input,
    Const,
    Array,
    /// Mutable scalar local introduced implicitly on first use. Scalars are
    /// zero-initialised, matching the zero-initialisation of arrays.
    Scalar,
}

#[derive(Clone, Debug)]
pub struct InputInfo {
    pub name: String,
    pub lo: i64,
    pub hi: i64,
    pub name_span: Span,
}

#[derive(Clone, Debug)]
pub struct ArrayInfo {
    pub name: String,
    pub len: u64,
    pub name_span: Span,
}

/// Fully resolved symbol table handed to every downstream component.
#[derive(Clone, Debug, Default)]
pub struct ProgramInfo {
    pub inputs: Vec<InputInfo>,
    pub consts: HashMap<String, i64>,
    pub arrays: HashMap<String, ArrayInfo>,
    /// Implicitly-declared mutable scalars, in first-use order. All start at 0.
    pub scalars: Vec<String>,
    kinds: HashMap<String, DeclKind>,
}

impl ProgramInfo {
    pub fn kind_of(&self, name: &str) -> Option<DeclKind> {
        self.kinds.get(name).copied()
    }
    pub fn array_len(&self, name: &str) -> Option<u64> {
        self.arrays.get(name).map(|a| a.len)
    }

    fn introduce_scalar(&mut self, name: &str) {
        self.kinds.entry(name.to_string()).or_insert_with(|| {
            self.scalars.push(name.to_string());
            DeclKind::Scalar
        });
    }
}

pub fn resolve(program: &Program) -> Result<ProgramInfo, Vec<ValidationError>> {
    let mut info = ProgramInfo::default();
    let mut errs = Vec::new();

    for decl in &program.decls {
        if info.kinds.contains_key(decl.name()) {
            errs.push(ValidationError {
                message: format!("duplicate declaration of `{}`", decl.name()),
                span: decl.span(),
            });
            continue;
        }
        info.kinds.insert(decl.name().to_string(), match decl {
            Decl::Input { .. } => DeclKind::Input,
            Decl::Const { .. } => DeclKind::Const,
            Decl::Array { .. } => DeclKind::Array,
        });
        match decl {
            Decl::Input {
                name, lo, hi, name_span, ..
            } => info.inputs.push(InputInfo {
                name: name.clone(),
                lo: *lo,
                hi: *hi,
                name_span: *name_span,
            }),
            Decl::Const { name, value, .. } => {
                info.consts.insert(name.clone(), *value);
            }
            Decl::Array { name, len, name_span, .. } => {
                if *len == 0 {
                    errs.push(ValidationError {
                        message: format!("array `{name}` must have length at least 1"),
                        span: decl.span(),
                    });
                }
                if *len > MAX_ARRAY_LEN {
                    errs.push(ValidationError {
                        message: format!(
                            "array `{name}` length {len} exceeds MAX_ARRAY_LEN {MAX_ARRAY_LEN}"
                        ),
                        span: decl.span(),
                    });
                }
                info.arrays.insert(
                    name.clone(),
                    ArrayInfo {
                        name: name.clone(),
                        len: *len,
                        name_span: *name_span,
                    },
                );
            }
        }
    }

    walk_block(&program.body, &mut info, &mut errs);
    if errs.is_empty() {
        Ok(info)
    } else {
        Err(errs)
    }
}

fn require_array(
    info: &mut ProgramInfo,
    name: &str,
    span: Span,
    errs: &mut Vec<ValidationError>,
) {
    match info.kind_of(name) {
        Some(DeclKind::Array) => {}
        Some(_) => errs.push(ValidationError {
            message: format!("`{name}` is not an array; indexing requires an array declaration"),
            span,
        }),
        None => errs.push(ValidationError {
            message: format!("undeclared name `{name}` (implicit scalars cannot be indexed)"),
            span,
        }),
    }
}

fn walk_block(b: &Block, info: &mut ProgramInfo, errs: &mut Vec<ValidationError>) {
    for s in &b.stmts {
        walk_stmt(s, info, errs);
    }
}

fn walk_stmt(s: &Stmt, info: &mut ProgramInfo, errs: &mut Vec<ValidationError>) {
    match s {
        Stmt::Block(b) => walk_block(b, info, errs),
        Stmt::Assign { target, value, .. } => {
            walk_expr(value, info, errs);
            match info.kind_of(&target.name) {
                Some(DeclKind::Const) => errs.push(ValidationError {
                    message: format!("cannot assign to const `{}`", target.name),
                    span: target.name_span,
                }),
                Some(DeclKind::Array) => match &target.index {
                    Some(idx) => walk_expr(idx, info, errs),
                    None => errs.push(ValidationError {
                        message: format!(
                            "cannot assign whole array `{}`; write `{}[index] = ...`",
                            target.name, target.name
                        ),
                        span: target.name_span,
                    }),
                },
                Some(DeclKind::Input) => {
                    if target.index.is_some() {
                        errs.push(ValidationError {
                            message: format!("cannot index scalar input `{}`", target.name),
                            span: target.name_span,
                        });
                    }
                }
                Some(DeclKind::Scalar) => {
                    if target.index.is_some() {
                        errs.push(ValidationError {
                            message: format!("cannot index scalar `{}`", target.name),
                            span: target.name_span,
                        });
                    }
                }
                None => {
                    // Implicit scalar declaration at first assignment.
                    if target.index.is_some() {
                        errs.push(ValidationError {
                            message: format!(
                                "undeclared array `{}`; declare it with `array {}[n];`",
                                target.name, target.name
                            ),
                            span: target.name_span,
                        });
                    } else {
                        info.introduce_scalar(&target.name);
                    }
                }
            }
        }
        Stmt::If {
            cond,
            then,
            otherwise,
            ..
        } => {
            walk_expr(cond, info, errs);
            walk_stmt(then, info, errs);
            if let Some(e) = otherwise {
                walk_stmt(e, info, errs);
            }
        }
        Stmt::While { cond, body, .. } => {
            walk_expr(cond, info, errs);
            walk_stmt(body, info, errs);
        }
        Stmt::Assert { cond, .. } => walk_expr(cond, info, errs),
        Stmt::Skip { .. } => {}
    }
}

fn walk_expr(e: &Expr, info: &mut ProgramInfo, errs: &mut Vec<ValidationError>) {
    match &e.kind {
        ExprKind::Int(_) => {}
        ExprKind::Var(name) => {
            // Any scalar read of an unknown name implicitly declares a
            // zero-initialised scalar (inputs and consts stay explicit).
            if info.kind_of(name).is_none() {
                info.introduce_scalar(name);
            }
        }
        ExprKind::ArrayRead { name, index } => {
            require_array(info, name, e.span, errs);
            walk_expr(index, info, errs);
        }
        ExprKind::Unary { inner, .. } => walk_expr(inner, info, errs),
        ExprKind::Binary { lhs, rhs, .. } => {
            walk_expr(lhs, info, errs);
            walk_expr(rhs, info, errs);
        }
    }
}
