//! Abstract state: maps from scalars to intervals and from arrays to
//! (length, weak-updated element interval), plus an explicit bottom flag.

use super::interval::Interval;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArrayAbs {
    pub len: i64,
    pub elem: Interval,
}

#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct AbsState {
    #[serde(default)]
    pub vars: BTreeMap<String, Interval>,
    #[serde(default)]
    pub arrays: BTreeMap<String, ArrayAbs>,
    #[serde(default)]
    pub is_bottom: bool,
}

impl AbsState {
    pub fn new() -> Self {
        AbsState::default()
    }

    pub fn bottom() -> Self {
        AbsState {
            vars: BTreeMap::new(),
            arrays: BTreeMap::new(),
            is_bottom: true,
        }
    }

    fn var_at(vars: &BTreeMap<String, Interval>, name: &str) -> Interval {
        vars.get(name).copied().unwrap_or(Interval::Bottom)
    }

    pub fn get_var(&self, name: &str) -> Interval {
        if self.is_bottom {
            return Interval::Bottom;
        }
        Self::var_at(&self.vars, name)
    }

    /// Assign and propagate bottom: assigning the empty interval empties the
    /// whole state (no concrete execution reaches past this point).
    pub fn set_var(&mut self, name: &str, v: Interval) {
        if self.is_bottom {
            return;
        }
        if v.is_bottom() {
            self.is_bottom = true;
            return;
        }
        self.vars.insert(name.to_string(), v);
    }

    pub fn join(&self, other: &AbsState) -> AbsState {
        if self.is_bottom {
            return other.clone();
        }
        if other.is_bottom {
            return self.clone();
        }
        let mut vars = BTreeMap::new();
        for k in self.vars.keys().chain(other.vars.keys()) {
            let v = Self::var_at(&self.vars, k).join(Self::var_at(&other.vars, k));
            vars.insert(k.clone(), v);
        }
        let mut arrays = BTreeMap::new();
        for k in self.arrays.keys().chain(other.arrays.keys()) {
            match (self.arrays.get(k), other.arrays.get(k)) {
                // Length mismatch on same name cannot happen in a well-formed
                // program (arrays are declared once).
                (Some(x), Some(y)) => {
                    arrays.insert(
                        k.clone(),
                        ArrayAbs {
                            len: x.len,
                            elem: x.elem.join(y.elem),
                        },
                    );
                }
                (Some(x), None) => {
                    arrays.insert(k.clone(), x.clone());
                }
                (None, Some(y)) => {
                    arrays.insert(k.clone(), y.clone());
                }
                (None, None) => {}
            }
        }
        AbsState {
            vars,
            arrays,
            is_bottom: false,
        }
    }

    pub fn widen(&self, other: &AbsState) -> AbsState {
        if self.is_bottom {
            return other.clone();
        }
        if other.is_bottom {
            return self.clone();
        }
        let mut vars = BTreeMap::new();
        for k in self.vars.keys().chain(other.vars.keys()) {
            let v = Self::var_at(&self.vars, k).widen(Self::var_at(&other.vars, k));
            vars.insert(k.clone(), v);
        }
        let mut arrays = BTreeMap::new();
        for k in self.arrays.keys().chain(other.arrays.keys()) {
            let merged = match (self.arrays.get(k), other.arrays.get(k)) {
                (Some(x), Some(y)) => ArrayAbs {
                    len: x.len,
                    elem: x.elem.widen(y.elem),
                },
                (Some(x), None) => x.clone(),
                (None, Some(y)) => y.clone(),
                (None, None) => continue,
            };
            arrays.insert(k.clone(), merged);
        }
        AbsState {
            vars,
            arrays,
            is_bottom: false,
        }
    }

    pub fn narrow(&self, other: &AbsState) -> AbsState {
        if self.is_bottom || other.is_bottom {
            return AbsState::bottom();
        }
        let mut vars = BTreeMap::new();
        for k in self.vars.keys().chain(other.vars.keys()) {
            let v = Self::var_at(&self.vars, k).narrow(Self::var_at(&other.vars, k));
            vars.insert(k.clone(), v);
        }
        let mut arrays = BTreeMap::new();
        for k in self.arrays.keys().chain(other.arrays.keys()) {
            let merged = match (self.arrays.get(k), other.arrays.get(k)) {
                (Some(x), Some(y)) => ArrayAbs {
                    len: x.len,
                    elem: x.elem.narrow(y.elem),
                },
                (Some(x), None) => x.clone(),
                (None, Some(y)) => y.clone(),
                (None, None) => continue,
            };
            arrays.insert(k.clone(), merged);
        }
        let s = AbsState {
            vars,
            arrays,
            is_bottom: false,
        };
        if s.vars.values().any(Interval::is_bottom) {
            AbsState::bottom()
        } else {
            s
        }
    }

    /// Pointwise subset, used for fixpoint convergence tests and evidence
    /// verification. A binding missing in `other` acts as bottom there.
    pub fn subset_of(&self, other: &AbsState) -> bool {
        if self.is_bottom {
            return true;
        }
        if other.is_bottom {
            return false;
        }
        for (k, v) in &self.vars {
            if !v.subset_of(&Self::var_at(&other.vars, k)) {
                return false;
            }
        }
        for (k, a) in &self.arrays {
            match other.arrays.get(k) {
                None => return false,
                Some(b) if a.len != b.len => return false,
                Some(b) => {
                    if !a.elem.subset_of(&b.elem) {
                        return false;
                    }
                }
            }
        }
        true
    }

    /// Structural validity used by the evidence checker.
    pub fn intervals_well_formed(&self) -> bool {
        self.vars.values().all(|i| i.is_well_formed())
            && self.arrays.values().all(|a| a.elem.is_well_formed())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bottom_laws() {
        let s = AbsState::new();
        let b = AbsState::bottom();
        assert_eq!(s.join(&b), s);
        assert!(b.subset_of(&s));
        assert!(!s.subset_of(&b));
    }
}
