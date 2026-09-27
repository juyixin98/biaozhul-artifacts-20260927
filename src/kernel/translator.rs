//! Translates type-checked AST expressions into paired representations:
//! Z3 bitvector/boolean terms for solving, and [`NInt`]/[`NBool`] trees for
//! the independent native evaluator.
//!
//! Division/remainder terms collect an ordered list of *divisor guards*.
//! Every guard is a fork event the engine must decide before the enclosing
//! statement proceeds: divisor == 0 is a failure, divisor != 0 continues.
//! The translated divisor itself is wrapped with an `ite(d != 0, d, 1)`
//! fallback so the term stays well-formed on both sides of the fork.

use std::collections::{HashMap, HashSet};

use z3::ast::{Ast, Bool, BV};
use z3::Context;

use crate::evidence::native::{NBinOp, NBool, NInt, NUnOp, SsaEnv};
use crate::kernel::state::SymState;
use crate::lang::ast::*;
use crate::lang::types::Type;

/// One divisor-safety occurrence, in source evaluation order.
pub struct DivisorGuard<'ctx> {
    pub divisor_node_id: u32,
    pub divisor_line: u32,
    /// The divisor term (machine width bits).
    pub divisor: BV<'ctx>,
    pub divisor_native: NInt,
    /// Literal "divisor == 0".
    pub zero: Bool<'ctx>,
    pub zero_native: NBool,
    /// Width in bits.
    pub width: u32,
}

pub struct Translator<'a, 'ctx> {
    ctx: &'ctx Context,
    state: &'a mut SymState<'ctx>,
    /// Names of program parameters (version-0 bindings that read inputs).
    params: HashSet<String>,
    /// SSA substitution environment: `name#version` -> (Z3 term, native term).
    /// Variables are *substituted* with their defining expression rather than
    /// constrained by equality, so the solver only ever sees parameters as
    /// free constants.
    terms: HashMap<String, (BV<'ctx>, NInt)>,
}

impl<'a, 'ctx> Translator<'a, 'ctx> {
    pub fn new(ctx: &'ctx Context, state: &'a mut SymState<'ctx>, program: &Program) -> Self {
        let mut terms = HashMap::new();
        for p in &program.params {
            let key = format!("{}#0", p.name);
            terms.insert(
                key.clone(),
                (
                    BV::new_const(ctx, key, p.ty.bits()),
                    NInt::Param(p.ty.bits(), p.name.clone()),
                ),
            );
        }
        Translator {
            ctx,
            state,
            params: program.params.iter().map(|p| p.name.clone()).collect(),
            terms,
        }
    }

    pub fn ctx(&self) -> &'ctx Context {
        self.ctx
    }

    #[allow(dead_code)]
    fn is_param(&self, name: &str) -> bool {
        self.params.contains(name)
    }

    // -- state delegation used by the engine -------------------------------

    pub fn state_push_block(&mut self) {
        self.state.push_block();
    }
    pub fn state_pop_block(&mut self) {
        self.state.pop_block();
    }
    pub fn state_add_constraint(&mut self, z3: Bool<'ctx>, native: NBool) {
        self.state.add_constraint(z3, native);
    }
    pub fn state_pc_native(&self) -> Vec<NBool> {
        self.state.pc_native.clone()
    }
    pub fn state_ssa(&self) -> SsaEnv {
        self.state.native_ssa.clone()
    }
    pub fn state_declare(&mut self, name: &str, ty: Type, z3: BV<'ctx>, native: NInt) {
        let version = self.state.declare(name, ty);
        self.terms
            .insert(format!("{name}#{version}"), (z3, native.clone()));
        self.state.bind_native(name, version, native);
    }
    pub fn state_assign(&mut self, name: &str, z3: BV<'ctx>, native: NInt) {
        let version = self.state.assign_version(name);
        self.terms
            .insert(format!("{name}#{version}"), (z3, native.clone()));
        self.state.bind_native(name, version, native);
    }

    fn const_bv(&self, v: u64, width: u32) -> BV<'ctx> {
        BV::from_u64(self.ctx, v, width)
    }

    /// Translate an integer-valued expression, collecting divisor guards.
    pub fn int(
        &mut self,
        e: &Expr,
        guards: &mut Vec<DivisorGuard<'ctx>>,
    ) -> (BV<'ctx>, NInt) {
        match &e.kind {
            ExprKind::Lit(lit) => {
                let ty = lit.suffix.or(e.ty).expect("inferred width");
                let v = lit.value & ty.mask();
                (
                    self.const_bv(v, ty.bits()),
                    NInt::Const(ty.bits(), v),
                )
            }
            ExprKind::Var(name) => {
                let (version, _ty) = self.state.current(name).expect("type-checked var");
                // Substitution: every variable occurrence is replaced by the
                // term it was bound to (parameters map to free constants).
                let key = format!("{name}#{version}");
                let Some((z, n)) = self.terms.get(&key) else {
                    panic!("SSA binding {key} missing from substitution environment");
                };
                (z.clone(), n.clone())
            }
            ExprKind::Un(op, inner) => {
                let width = e.ty.expect("inferred width").bits();
                let (zv, nv) = self.int(inner, guards);
                let (z, n) = match op {
                    UnOp::Neg => (zv.bvneg(), NInt::Un(width, NUnOp::Neg, Box::new(nv))),
                    UnOp::BitNot => (!&zv, NInt::Un(width, NUnOp::BitNot, Box::new(nv))),
                    UnOp::Not => panic!("logical not cannot be integer-valued"),
                };
                (z, n)
            }
            ExprKind::Bin(op, a, b) => {
                let width = e.ty.expect("inferred width").bits();
                self.int_bin(e.id, e.span.line, width, *op, a, b, guards)
            }
            ExprKind::BoolLit(_) => panic!("boolean literal in integer position"),
        }
    }

    fn int_bin(
        &mut self,
        node_id: NodeId,
        line: u32,
        width: u32,
        op: BinOp,
        a: &Expr,
        b: &Expr,
        guards: &mut Vec<DivisorGuard<'ctx>>,
    ) -> (BV<'ctx>, NInt) {
        // Left operand is evaluated first, so any nested guard keeps source
        // order.
        let (za, na) = self.int(a, guards);
        let (zb, nb) = self.int(b, guards);
        let z = match op {
            BinOp::Add => za.bvadd(&zb),
            BinOp::Sub => za.bvsub(&zb),
            BinOp::Mul => za.bvmul(&zb),
            BinOp::BitAnd => za & zb,
            BinOp::BitOr => za | zb,
            BinOp::BitXor => za ^ zb,
            BinOp::Shl => za.bvshl(&zb),
            BinOp::Shr => za.bvlshr(&zb),
            BinOp::Div | BinOp::Rem => {
                // Guard first: divisor != 0 is required to continue.
                let zero_lit = self.const_bv(0, width);
                let zero = zb._eq(&zero_lit);
                let zero_native = NBool::Cmp(
                    NBinOp::Eq,
                    Box::new(nb.clone()),
                    Box::new(NInt::Const(width, 0)),
                );
                guards.push(DivisorGuard {
                    divisor_node_id: b.id.get(),
                    divisor_line: b.span.line,
                    divisor: zb.clone(),
                    divisor_native: nb.clone(),
                    zero: zero.clone(),
                    zero_native: zero_native.clone(),
                    width,
                });
                let safe_divisor = zero.ite(&self.const_bv(1, width), &zb);
                match op {
                    BinOp::Div => za.bvudiv(&safe_divisor),
                    BinOp::Rem => za.bvurem(&safe_divisor),
                    _ => unreachable!(),
                }
            }
            _ => panic!("{op:?} is not integer-valued at int_bin"),
        };
        let n = NInt::Bin(width, map_int_op(op), Box::new(na), Box::new(nb));
        let _ = (node_id, line);
        (z, n)
    }

    /// Translate a boolean-valued expression, collecting divisor guards.
    pub fn bool_(
        &mut self,
        e: &Expr,
        guards: &mut Vec<DivisorGuard<'ctx>>,
    ) -> (Bool<'ctx>, NBool) {
        match &e.kind {
            ExprKind::BoolLit(b) => (Bool::from_bool(self.ctx, *b), NBool::Const(*b)),
            ExprKind::Un(UnOp::Not, inner) => {
                let (z, n) = self.bool_(inner, guards);
                (!&z, NBool::Not(Box::new(n)))
            }
            ExprKind::Bin(op, a, b) if matches!(op, BinOp::LAnd | BinOp::LOr) => {
                let (za, na) = self.bool_(a, guards);
                let (zb, nb) = self.bool_(b, guards);
                match op {
                    BinOp::LAnd => (
                        Bool::and(self.ctx, &[&za, &zb]),
                        NBool::LAnd(Box::new(na), Box::new(nb)),
                    ),
                    BinOp::LOr => (
                        Bool::or(self.ctx, &[&za, &zb]),
                        NBool::LOr(Box::new(na), Box::new(nb)),
                    ),
                    _ => unreachable!(),
                }
            }
            ExprKind::Bin(op, a, b) => {
                let (za, na) = self.int(a, guards);
                let (zb, nb) = self.int(b, guards);
                let z = match op {
                    BinOp::Eq => za._eq(&zb),
                    BinOp::Ne => za._eq(&zb).not(),
                    BinOp::Lt => za.bvult(&zb),
                    BinOp::Le => za.bvule(&zb),
                    BinOp::Gt => za.bvugt(&zb),
                    BinOp::Ge => za.bvuge(&zb),
                    _ => panic!("{op:?} not a comparison at bool"),
                };
                let n = NBool::Cmp(map_cmp_op(*op), Box::new(na), Box::new(nb));
                (z, n)
            }
            ExprKind::Lit(_) | ExprKind::Var(_) => {
                panic!("integer used directly as boolean (type checker should reject)")
            }
            ExprKind::Un(..) => panic!("unary integer op used as boolean"),
        }
    }
}

fn map_int_op(op: BinOp) -> NBinOp {
    match op {
        BinOp::Add => NBinOp::Add,
        BinOp::Sub => NBinOp::Sub,
        BinOp::Mul => NBinOp::Mul,
        BinOp::Div => NBinOp::Div,
        BinOp::Rem => NBinOp::Rem,
        BinOp::BitAnd => NBinOp::BitAnd,
        BinOp::BitOr => NBinOp::BitOr,
        BinOp::BitXor => NBinOp::BitXor,
        BinOp::Shl => NBinOp::Shl,
        BinOp::Shr => NBinOp::Shr,
        other => panic!("map_int_op on non-int op {other:?}"),
    }
}

fn map_cmp_op(op: BinOp) -> NBinOp {
    match op {
        BinOp::Eq => NBinOp::Eq,
        BinOp::Ne => NBinOp::Ne,
        BinOp::Lt => NBinOp::Lt,
        BinOp::Le => NBinOp::Le,
        BinOp::Gt => NBinOp::Gt,
        BinOp::Ge => NBinOp::Ge,
        other => panic!("map_cmp_op on non-comparison {other:?}"),
    }
}
