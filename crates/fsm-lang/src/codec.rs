//! Canonical state encoding (deduplication keys).
//!
//! Each variable is first normalized to a small non-negative integer digit
//! (`bool`: 0/1, int range: `v - lo`, enum: its ordinal). The digit vector is
//! then mapped to a single `u64` via mixed radix using the precomputed
//! strides. The mapping is bijective over the product domain, so two states
//! have the same code iff they are equal — no hashing collisions are
//! possible. The build step guarantees the product fits in `u64`.

use crate::error::{BuildError, BuildErrorKind};
use crate::system::{Domain, System, Value};

/// Normalize one value to its digit.
pub fn digit(domain: &Domain, v: &Value) -> Result<u64, BuildError> {
    match (domain, v) {
        (Domain::Bool, Value::Bool(b)) => Ok(*b as u64),
        (Domain::IntRange { lo, .. }, Value::Int(i)) => {
            Ok(u64::try_from(i - lo).expect("in-range values have non-negative digits"))
        }
        (Domain::Enum { variants }, Value::Int(i)) => {
            if *i >= 0 && (*i as usize) < variants.len() {
                Ok(*i as u64)
            } else {
                Err(BuildError::new(
                    BuildErrorKind::InvalidDomain,
                    "enum ordinal outside variant list",
                ))
            }
        }
        _ => Err(BuildError::new(
            BuildErrorKind::TypeMismatch,
            "value does not match its declared domain",
        )),
    }
}

impl System {
    /// Canonical mixed-radix code of a state.
    pub fn encode(&self, st: &[Value]) -> u64 {
        let mut code: u64 = 0;
        for (v, val) in self.vars.iter().zip(st) {
            let d = digit(&v.domain, val).expect("reachable states are well-typed");
            code = code
                .checked_add(v.stride.checked_mul(d).expect("code overflow"))
                .expect("code overflow");
        }
        code
    }

    /// Inverse of [`System::encode`].
    pub fn decode(&self, mut code: u64) -> Vec<Value> {
        let mut st = Vec::with_capacity(self.vars.len());
        for v in &self.vars {
            let d = code / v.stride;
            code %= v.stride;
            let val = match &v.domain {
                Domain::Bool => Value::Bool(d != 0),
                Domain::IntRange { lo, .. } => Value::Int(lo + d as i64),
                Domain::Enum { variants } => Value::Int(d.min(variants.len() as u64 - 1) as i64),
            };
            st.push(val);
        }
        st
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::eval::build_system;
    use crate::parser::parse_system;

    fn sys(src: &str) -> System {
        build_system(parse_system(src).unwrap()).unwrap()
    }

    #[test]
    fn encode_decode_roundtrip_is_bijective_on_counter() {
        let s = sys("system c { var { x: int[0..3] } init { x := 0 } transition i { guard: x<3; then: x:=x+1 } }");
        for i in 0..=3i64 {
            let st = vec![Value::Int(i)];
            let code = s.encode(&st);
            assert_eq!(s.decode(code), st);
        }
    }

    #[test]
    fn mixed_radix_is_order_preserving_and_unique() {
        let s = sys("system m { var { a: bool; b: int[0..2] } init { a := false, b := 0 } transition t { guard: true; then: a := !a } }");
        let mut codes = std::collections::HashSet::new();
        for a in [false, true] {
            for b in 0..=2i64 {
                let st = vec![Value::Bool(a), Value::Int(b)];
                assert!(codes.insert(s.encode(&st)), "code collision");
            }
        }
        assert_eq!(codes.len(), 6);
        // stride of last var is 1, first var stride equals second domain size
        assert_eq!(s.vars[1].stride, 1);
        assert_eq!(s.vars[0].stride, 3);
    }
}
