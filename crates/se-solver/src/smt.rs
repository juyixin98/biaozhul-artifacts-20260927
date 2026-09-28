//! Lowering of [`Term`] into SMT-LIB 2 text (QF_BV).
//!
//! Emission is deliberately naive (full parenthesization, no let-sharing). Programs
//! here are small and bounded; clarity and exactness of generated formulas take
//! precedence over term size.

use se_lang::Width;

use crate::term::{
    BoolBinOp, BoolUnOp, BvBinOp, BvUnOp, CmpRel, Sort, Term, TermNode,
};

/// Names must be safe to quote as SMT-LIB `|...|` symbols. We accept a conservative
/// ASCII identifier alphabet; anything else is rejected before it reaches a solver.
pub fn validate_name(name: &str) -> bool {
    !name.is_empty()
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.'))
}

/// Quote a name as an SMT-LIB quoted symbol (`|...|`). Inputs already passed
/// [`validate_name`], so `|` and `\` cannot occur.
fn sym(name: &str) -> String {
    format!("|{name}|")
}

pub struct SmtEmitter {
    width: Width,
}

impl SmtEmitter {
    pub fn new(width: Width) -> Self {
        SmtEmitter { width }
    }

    pub fn width(&self) -> Width {
        self.width
    }

    /// Emit an SMT-LIB expression for `t`; panics only on sort mismatch bugs in the
    /// engine (which constructs terms through this module's typed helpers in
    /// practice — here we defensively return an error instead).
    pub fn emit(&self, t: &Term) -> Result<String, SortError> {
        let mut out = String::new();
        self.emit_into(t, &mut out)?;
        Ok(out)
    }

    fn emit_into(&self, t: &Term, out: &mut String) -> Result<(), SortError> {
        match t.as_ref() {
            TermNode::BvConst(n) => {
                out.push_str(&format!("(_ bv{} {})", n & self.width.mask_u64(), self.width.bits()));
            }
            TermNode::BvVar(name) => {
                if !validate_name(name) {
                    return Err(SortError::BadName(name.clone()));
                }
                out.push_str(&sym(name));
            }
            TermNode::BvUn(op, a) => {
                self.expect(a, Sort::Bv)?;
                let op = match op {
                    BvUnOp::Neg => "bvneg",
                    BvUnOp::Not => "bvnot",
                };
                out.push_str(&format!("({op} "));
                self.emit_into(a, out)?;
                out.push(')');
            }
            TermNode::BvBin(op, a, b) => {
                self.expect(a, Sort::Bv)?;
                self.expect(b, Sort::Bv)?;
                let op = match op {
                    BvBinOp::Add => "bvadd",
                    BvBinOp::Sub => "bvsub",
                    BvBinOp::Mul => "bvmul",
                    BvBinOp::Udiv => "bvudiv",
                    BvBinOp::Urem => "bvurem",
                    BvBinOp::Sdiv => "bvsdiv",
                    BvBinOp::Srem => "bvsrem",
                    BvBinOp::And => "bvand",
                    BvBinOp::Or => "bvor",
                    BvBinOp::Xor => "bvxor",
                    BvBinOp::Shl => "bvshl",
                    BvBinOp::LShr => "bvlshr",
                    BvBinOp::AShr => "bvashr",
                };
                out.push_str(&format!("({op} "));
                self.emit_into(a, out)?;
                out.push(' ');
                self.emit_into(b, out)?;
                out.push(')');
            }
            TermNode::BvIte(c, th, el) => {
                self.expect(c, Sort::Bool)?;
                self.expect(th, Sort::Bv)?;
                self.expect(el, Sort::Bv)?;
                out.push_str("(ite ");
                self.emit_into(c, out)?;
                out.push(' ');
                self.emit_into(th, out)?;
                out.push(' ');
                self.emit_into(el, out)?;
                out.push(')');
            }
            TermNode::BoolConst(b) => out.push_str(if *b { "true" } else { "false" }),
            TermNode::BoolUn(op, a) => {
                self.expect(a, Sort::Bool)?;
                let op = match op {
                    BoolUnOp::Not => "not",
                };
                out.push_str(&format!("({op} "));
                self.emit_into(a, out)?;
                out.push(')');
            }
            TermNode::BoolBin(op, a, b) => {
                self.expect(a, Sort::Bool)?;
                self.expect(b, Sort::Bool)?;
                let op = match op {
                    BoolBinOp::And => "and",
                    BoolBinOp::Or => "or",
                    BoolBinOp::Xor => "xor",
                    BoolBinOp::Implies => "=>",
                };
                out.push_str(&format!("({op} "));
                self.emit_into(a, out)?;
                out.push(' ');
                self.emit_into(b, out)?;
                out.push(')');
            }
            TermNode::Rel(rel, a, b) => {
                self.expect(a, Sort::Bv)?;
                self.expect(b, Sort::Bv)?;
                let op = match rel {
                    CmpRel::Eq => "=",
                    CmpRel::Ne => "distinct",
                    CmpRel::Ult => "bvult",
                    CmpRel::Ule => "bvule",
                    CmpRel::Ugt => "bvugt",
                    CmpRel::Uge => "bvuge",
                    CmpRel::Slt => "bvslt",
                    CmpRel::Sle => "bvsle",
                    CmpRel::Sgt => "bvsgt",
                    CmpRel::Sge => "bvsge",
                };
                out.push_str(&format!("({op} "));
                self.emit_into(a, out)?;
                out.push(' ');
                self.emit_into(b, out)?;
                out.push(')');
            }
        }
        Ok(())
    }

    fn expect(&self, t: &Term, want: Sort) -> Result<(), SortError> {
        let got = t.sort();
        if got == want {
            Ok(())
        } else {
            Err(SortError::Mismatch { want, got })
        }
    }
}

#[derive(Clone, Debug, thiserror::Error)]
pub enum SortError {
    #[error("term sort mismatch: expected {want:?}, got {got:?}")]
    Mismatch { want: Sort, got: Sort },
    #[error("illegal SMT identifier: {0:?}")]
    BadName(String),
}

/// Build a complete SMT-LIB 2 check-sat/get-model script.
///
/// * `inputs` declares the free bitvectors (program inputs);
/// * `assumptions` are all asserted Bool terms;
/// * `timeout_ms` sets the solver's soft per-query timeout.
pub fn build_query(
    width: Width,
    inputs: &[String],
    assumptions: &[Term],
    timeout_ms: u32,
) -> Result<String, SortError> {
    let em = SmtEmitter::new(width);
    let mut s = String::new();
    s.push_str("(set-option :print-success false)\n");
    s.push_str("(set-option :produce-models true)\n");
    if timeout_ms > 0 {
        s.push_str(&format!("(set-option :timeout {timeout_ms})\n"));
    }
    for name in inputs {
        if !validate_name(name) {
            return Err(SortError::BadName(name.clone()));
        }
        s.push_str(&format!(
            "(declare-fun {sym} () (_ BitVec {bits}))\n",
            sym = sym(name),
            bits = width.bits()
        ));
    }
    for a in assumptions {
        em.expect(a, Sort::Bool)?;
        s.push_str("(assert ");
        em.emit_into(a, &mut s)?;
        s.push_str(")\n");
    }
    s.push_str("(check-sat)\n");
    s.push_str("(get-model)\n");
    for name in inputs {
        s.push_str(&format!("(get-value ({sym}))\n", sym = sym(name)));
    }
    s.push_str("(exit)\n");
    Ok(s)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::term::{bv, bv_bin, nonzero, rel, var, BvBinOp, CmpRel};

    #[test]
    fn emits_bvformula() {
        let width = Width::W8;
        let x = var("x");
        let y = var("y");
        let add = bv_bin(BvBinOp::Add, x.clone(), y);
        let c = rel(CmpRel::Ugt, add, bv(10));
        let q = build_query(width, &["x".into(), "y".into()], &[c, nonzero(x)], 1000).unwrap();
        assert!(q.contains("(declare-fun |x| () (_ BitVec 8))"));
        assert!(q.contains("(assert (bvugt (bvadd |x| |y|) (_ bv10 8)))"));
        assert!(q.contains("(assert (distinct |x| (_ bv0 8)))"));
        assert!(q.contains("(check-sat)"));
        assert!(q.contains("(get-value (|x|))"));
    }

    #[test]
    fn rejects_bad_identifiers() {
        assert!(!validate_name("a b"));
        assert!(!validate_name("|x|"));
        assert!(validate_name("x_1.y-2"));
    }
}
