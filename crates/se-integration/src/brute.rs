//! An **independent** test-only SMT solver.
//!
//! It does not reuse any engine/solver reasoning. It parses the SMT-LIB 2 text emitted
//! by `se-solver` with its own tiny s-expression reader and evaluates the QF_BV
//! constraints by brute-forcing the declared input domains. This makes the engine's
//! decisions testable without Z3 and provides a ground-truth checker written solely for
//! the tests.
//!
//! Scope on purpose: bitvector widths 8/16/32/64, the operators the language emits,
//! `and`/`or`/`not`/`=>`/`=`/`distinct` and the bv comparison/arithmetic/shift set.
//! Anything outside the test language reports `unknown` (never a fake answer).

use std::collections::BTreeMap;

use se_lang::Width;
use se_solver::{CheckResult, CheckStatus, SmtSolver};

#[derive(Clone, Debug)]
pub struct BruteSolver {
    pub width: Width,
    pub domains: BTreeMap<String, (u64, u64)>,
    /// Assignment cap; above it the solver answers unknown.
    pub cap: u64,
    pub queries: std::cell::RefCell<u64>,
}

impl BruteSolver {
    pub fn new(width: Width, domains: BTreeMap<String, (u64, u64)>, cap: u64) -> Self {
        BruteSolver {
            width,
            domains,
            cap,
            queries: std::cell::RefCell::new(0),
        }
    }
}

impl SmtSolver for BruteSolver {
    fn check(
        &self,
        _width: Width,
        _inputs: &[String],
        assumptions: &[se_solver::Term],
    ) -> Result<CheckResult, se_solver::SolverError> {
        *self.queries.borrow_mut() += 1;
        // Lower terms to SMT-LIB text (serialization only), then evaluate
        // independently. The brute solver never trusts term-level helpers.
        let em = se_solver::smt::SmtEmitter::new(self.width);
        let mut exprs = Vec::new();
        for a in assumptions {
            exprs.push(em.emit(a).expect("test terms are well-sorted"));
        }

        let mut parsed = Vec::new();
        for txt in &exprs {
            match parse_sexpr(txt) {
                Some(e) => parsed.push(e),
                None => {
                    return Ok(CheckResult {
                        status: CheckStatus::Unknown,
                        model: None,
                        reason: Some("brute solver parse miss".into()),
                        solver: self.name().to_string(),
                        solver_version: self.version().to_string(),
                        elapsed_ms: 0,
                    })
                }
            }
        }

        // Enumerate domain assignments (lexicographic by declaration order).
        let names: Vec<String> = self.domains.keys().cloned().collect();
        let bounds: Vec<(u64, u64)> = names.iter().map(|n| self.domains[n]).collect();
        let total: u128 = bounds
            .iter()
            .map(|(lo, hi)| (*hi as u128) - (*lo as u128) + 1)
            .product();
        if total > self.cap as u128 {
            return Ok(CheckResult {
                status: CheckStatus::Unknown,
                model: None,
                reason: Some(format!("brute cap {} < domain {total}", self.cap)),
                solver: self.name().to_string(),
                solver_version: self.version().to_string(),
                elapsed_ms: 0,
            });
        }

        let mut cur: Vec<u64> = bounds.iter().map(|(lo, _)| *lo).collect();
        let mask = self.width.mask_u64();
        loop {
            let env: BTreeMap<String, u64> = names
                .iter()
                .zip(cur.iter())
                .map(|(n, v)| (n.clone(), *v & mask))
                .collect();
            let mut ok = true;
            for e in &parsed {
                match eval_bool(e, &env, self.width) {
                    Some(true) => {}
                    Some(false) => {
                        ok = false;
                        break;
                    }
                    None => {
                        return Ok(CheckResult {
                            status: CheckStatus::Unknown,
                            model: None,
                            reason: Some("brute solver eval miss".into()),
                            solver: self.name().to_string(),
                            solver_version: self.version().to_string(),
                            elapsed_ms: 0,
                        })
                    }
                }
            }
            if ok {
                return Ok(CheckResult {
                    status: CheckStatus::Sat,
                    model: Some(env),
                    reason: None,
                    solver: self.name().to_string(),
                    solver_version: self.version().to_string(),
                    elapsed_ms: 0,
                });
            }
            // Mixed-radix increment.
            let mut carry = true;
            for k in (0..bounds.len()).rev() {
                if !carry {
                    break;
                }
                let (lo, hi) = bounds[k];
                if cur[k] < hi {
                    cur[k] += 1;
                    carry = false;
                } else {
                    cur[k] = lo;
                    carry = true;
                }
            }
            if carry {
                return Ok(CheckResult {
                    status: CheckStatus::Unsat,
                    model: None,
                    reason: None,
                    solver: self.name().to_string(),
                    solver_version: self.version().to_string(),
                    elapsed_ms: 0,
                });
            }
        }
    }

    fn name(&self) -> &str {
        "brute-indep"
    }
    fn version(&self) -> &str {
        "test-1.0"
    }
}

// ---------------------------------------------------------------------------
// Minimal s-expression reader
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
enum Sx {
    Sym(String),
    List(Vec<Sx>),
}

fn tokenize(s: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut buf = String::new();
    let mut chars = s.chars().peekable();
    // Quoted symbol |...| is kept as one token without bars.
    while let Some(c) = chars.next() {
        if c.is_whitespace() {
            if !buf.is_empty() {
                out.push(std::mem::take(&mut buf));
            }
        } else if c == '(' || c == ')' {
            if !buf.is_empty() {
                out.push(std::mem::take(&mut buf));
            }
            out.push(c.to_string());
        } else if c == '|' {
            let mut q = String::new();
            while let Some(&x) = chars.peek() {
                chars.next();
                if x == '|' {
                    break;
                }
                q.push(x);
            }
            out.push(format!("|{q}|"));
        } else {
            buf.push(c);
        }
    }
    if !buf.is_empty() {
        out.push(buf);
    }
    out
}

fn parse_sexpr(s: &str) -> Option<Sx> {
    let toks = tokenize(s);
    let mut pos = 0;
    fn one(toks: &[String], pos: &mut usize) -> Option<Sx> {
        let t = toks.get(*pos)?.clone();
        *pos += 1;
        if t == "(" {
            // Indexed/parameterized identifier `(_ bvN w)` collapses to one symbol so
            // evaluators see it as a literal token rather than an application.
            if toks.get(*pos).map(|s| s.as_str()) == Some("_") {
                *pos += 1;
                let mut joined = String::from("(_");
                loop {
                    let nxt = toks.get(*pos)?;
                    if nxt == ")" {
                        *pos += 1;
                        joined.push(')');
                        return Some(Sx::Sym(joined));
                    }
                    joined.push(' ');
                    joined.push_str(nxt);
                    *pos += 1;
                }
            }
            let mut items = Vec::new();
            loop {
                let nxt = toks.get(*pos)?;
                if nxt == ")" {
                    *pos += 1;
                    return Some(Sx::List(items));
                }
                items.push(one(toks, pos)?);
            }
        } else if t == ")" {
            None
        } else {
            Some(Sx::Sym(t))
        }
    }
    let e = one(&toks, &mut pos)?;
    if pos == toks.len() {
        Some(e)
    } else {
        None
    }
}

// ---------------------------------------------------------------------------
// Independent evaluator
// ---------------------------------------------------------------------------

fn mask(x: u128, w: Width) -> u64 {
    (x & w.mask_u64() as u128) as u64
}

fn signed(x: u64, w: Width) -> i128 {
    se_lang::bits::to_signed(x, w)
}

#[allow(clippy::manual_checked_ops)]
fn eval_bv(e: &Sx, env: &BTreeMap<String, u64>, w: Width) -> Option<u64> {
    match e {
        Sx::Sym(t) => {
            if let Some(name) = t.strip_prefix('|').and_then(|s| s.strip_suffix('|')) {
                env.get(name).copied()
            } else {
                bv_literal(t, w)
            }
        }
        Sx::List(items) => {
            let op = items.first().and_then(|s| match s {
                Sx::Sym(t) => Some(t.as_str()),
                _ => None,
            })?;
            let args = &items[1..];
            match op {
                "bvadd" => Some(mask(
                    eval_bv(&args[0], env, w)? as u128 + eval_bv(&args[1], env, w)? as u128,
                    w,
                )),
                "bvsub" => Some(mask(
                    eval_bv(&args[0], env, w)? as u128
                        + (eval_bv(&args[1], env, w)? as u128 ^ w.mask_u64() as u128)
                        + 1,
                    w,
                )),
                "bvmul" => Some(mask(
                    eval_bv(&args[0], env, w)? as u128 * eval_bv(&args[1], env, w)? as u128,
                    w,
                )),
                "bvudiv" => {
                    let b = eval_bv(&args[1], env, w)?;
                    if b == 0 {
                        Some(0) // matches reference edge semantics
                    } else {
                        Some(eval_bv(&args[0], env, w)? / b)
                    }
                }
                "bvurem" => {
                    let b = eval_bv(&args[1], env, w)?;
                    if b == 0 {
                        Some(0)
                    } else {
                        Some(eval_bv(&args[0], env, w)? % b)
                    }
                }
                "bvsdiv" | "bvsrem" => {
                    let a = signed(eval_bv(&args[0], env, w)?, w);
                    let b = signed(eval_bv(&args[1], env, w)?, w);
                    if b == 0 {
                        return Some(0);
                    }
                    let r = if op == "bvsdiv" {
                        a.wrapping_div(b)
                    } else {
                        a.wrapping_rem(b)
                    };
                    Some(se_lang::bits::from_signed(r, w))
                }
                "bvand" => Some(eval_bv(&args[0], env, w)? & eval_bv(&args[1], env, w)?),
                "bvor" => Some(eval_bv(&args[0], env, w)? | eval_bv(&args[1], env, w)?),
                "bvxor" => Some(eval_bv(&args[0], env, w)? ^ eval_bv(&args[1], env, w)?),
                "bvnot" => Some(!eval_bv(&args[0], env, w)? & w.mask_u64()),
                "bvneg" => Some(mask(0u128.wrapping_sub(eval_bv(&args[0], env, w)? as u128), w)),
                "bvshl" => {
                    let n = (eval_bv(&args[1], env, w)? & (w.bits() as u64 - 1)) as u32;
                    Some(mask((eval_bv(&args[0], env, w)? as u128) << n, w))
                }
                "bvlshr" => {
                    let n = (eval_bv(&args[1], env, w)? & (w.bits() as u64 - 1)) as u32;
                    Some(eval_bv(&args[0], env, w)? >> n)
                }
                "bvashr" => {
                    let a = signed(eval_bv(&args[0], env, w)?, w);
                    let n = (eval_bv(&args[1], env, w)? & (w.bits() as u64 - 1)) as u32;
                    Some(se_lang::bits::from_signed(a >> n, w))
                }
                "ite" => {
                    if eval_bool(&args[0], env, w)? {
                        eval_bv(&args[1], env, w)
                    } else {
                        eval_bv(&args[2], env, w)
                    }
                }
                _ => None,
            }
        }
    }
}

fn bv_literal(t: &str, w: Width) -> Option<u64> {
    if let Some(inner) = t.strip_prefix("(_").and_then(|s| s.strip_suffix(')')) {
        let mut it = inner.split_whitespace();
        let bv = it.next()?;
        let width_txt = it.next()?;
        let declared: u32 = width_txt.parse().ok()?;
        if declared != w.bits() {
            return None;
        }
        return bv.strip_prefix("bv")?.parse::<u64>().ok();
    }
    if let Some(h) = t.strip_prefix("#x") {
        return u64::from_str_radix(h, 16).ok();
    }
    if let Some(b) = t.strip_prefix("#b") {
        return u64::from_str_radix(b, 2).ok();
    }
    None
}

#[allow(clippy::too_many_lines)]
fn eval_bool(e: &Sx, env: &BTreeMap<String, u64>, w: Width) -> Option<bool> {
    match e {
        Sx::Sym(t) => match t.as_str() {
            "true" => Some(true),
            "false" => Some(false),
            _ => None,
        },
        Sx::List(items) => {
            let op = items.first().and_then(|s| match s {
                Sx::Sym(t) => Some(t.as_str()),
                _ => None,
            })?;
            let args = &items[1..];
            match op {
                "and" => {
                    for a in args {
                        if !eval_bool(a, env, w)? {
                            return Some(false);
                        }
                    }
                    Some(true)
                }
                "or" => {
                    for a in args {
                        if eval_bool(a, env, w)? {
                            return Some(true);
                        }
                    }
                    Some(false)
                }
                "xor" => Some(eval_bool(&args[0], env, w)? ^ eval_bool(&args[1], env, w)?),
                "not" => Some(!eval_bool(&args[0], env, w)?),
                "=>" => Some(!eval_bool(&args[0], env, w)? || eval_bool(&args[1], env, w)?),
                "=" => {
                    // Polymorphic: try bitvector first, fall back to booleans.
                    match (eval_bv(&args[0], env, w), eval_bv(&args[1], env, w)) {
                        (Some(a), Some(b)) => Some(a == b),
                        _ => Some(eval_bool(&args[0], env, w)? == eval_bool(&args[1], env, w)?),
                    }
                }
                "distinct" => {
                    match (eval_bv(&args[0], env, w), eval_bv(&args[1], env, w)) {
                        (Some(a), Some(b)) => Some(a != b),
                        _ => Some(eval_bool(&args[0], env, w)? != eval_bool(&args[1], env, w)?),
                    }
                }
                "bvult" => Some(eval_bv(&args[0], env, w)? < eval_bv(&args[1], env, w)?),
                "bvule" => Some(eval_bv(&args[0], env, w)? <= eval_bv(&args[1], env, w)?),
                "bvugt" => Some(eval_bv(&args[0], env, w)? > eval_bv(&args[1], env, w)?),
                "bvuge" => Some(eval_bv(&args[0], env, w)? >= eval_bv(&args[1], env, w)?),
                "bvslt" => Some(
                    signed(eval_bv(&args[0], env, w)?, w)
                        < signed(eval_bv(&args[1], env, w)?, w),
                ),
                "bvsle" => Some(
                    signed(eval_bv(&args[0], env, w)?, w)
                        <= signed(eval_bv(&args[1], env, w)?, w),
                ),
                "bvsgt" => Some(
                    signed(eval_bv(&args[0], env, w)?, w)
                        > signed(eval_bv(&args[1], env, w)?, w),
                ),
                "bvsge" => Some(
                    signed(eval_bv(&args[0], env, w)?, w)
                        >= signed(eval_bv(&args[1], env, w)?, w),
                ),
                "ite" => {
                    if eval_bool(&args[0], env, w)? {
                        eval_bool(&args[1], env, w)
                    } else {
                        eval_bool(&args[2], env, w)
                    }
                }
                _ => None,
            }
        }
    }
}

/// Construct a BruteSolver from a parsed program's declared domains.
pub fn solver_for(program: &se_lang::Program, cap: u64) -> BruteSolver {
    let domains = program
        .inputs
        .iter()
        .map(|i| (i.name.clone(), (i.low, i.high)))
        .collect();
    BruteSolver::new(program.width, domains, cap)
}
