//! Per-path symbolic state.
//!
//! Each explored path gets a fresh [`SymState`]: a versioned store mapping
//! program variables to monotonically increasing SSA versions, plus the path
//! condition in two representations at once — Z3 booleans for solving and
//! [`NBool`] trees for the solver-independent replay/enumeration oracle.

use std::collections::BTreeMap;

use z3::ast::Bool;
use z3::Context;

use crate::evidence::native::{NBool, NInt, SsaEnv, ssa_key};
use crate::lang::ast::Program;
use crate::lang::types::Type;

/// Versioned variable binding as seen inside one path run.
pub struct Versioned {
    pub ty: Type,
    pub version: u32,
}

/// One binding popped when leaving a block scope.
struct ScopeEntry {
    name: String,
    previous: Option<Versioned>,
}

pub struct SymState<'ctx> {
    /// Current SSA version counter per variable name.
    versions: BTreeMap<String, u32>,
    /// Current visible binding (type + version).
    store: BTreeMap<String, Versioned>,
    /// Native terms for every SSA binding produced so far (run-local; the
    /// engine merges them into a global table once the run finishes).
    pub native_ssa: SsaEnv,
    /// Z3 path condition (conjunction of all taken branch literals).
    pub pc_z3: Vec<Bool<'ctx>>,
    /// Native mirror of [`Self::pc_z3`].
    pub pc_native: Vec<NBool>,
    scope_stack: Vec<Vec<ScopeEntry>>,
}

impl<'ctx> SymState<'ctx> {
    /// Initial state: parameters are bound at version 0; Z3 constants for
    /// them are created lazily by the translator via [`Self::current`].
    pub fn new(_ctx: &'ctx Context, program: &Program) -> Self {
        let mut versions = BTreeMap::new();
        let mut store = BTreeMap::new();
        for p in &program.params {
            versions.insert(p.name.clone(), 0);
            store.insert(
                p.name.clone(),
                Versioned {
                    ty: p.ty,
                    version: 0,
                },
            );
        }
        SymState {
            versions,
            store,
            native_ssa: SsaEnv::new(),
            pc_z3: Vec::new(),
            pc_native: Vec::new(),
            scope_stack: Vec::new(),
        }
    }

    pub fn push_block(&mut self) {
        self.scope_stack.push(Vec::new());
    }

    /// Pop a block scope, reverting `let` bindings made inside it. Plain
    /// assignments to outer variables persist, as in ordinary lexical
    /// scoping. The version high-water mark is *not* rewound: SSA versions
    /// are unique per run, so sibling blocks declaring the same name get
    /// distinct versions.
    pub fn pop_block(&mut self) {
        let frame = self.scope_stack.pop().expect("block stack aligned");
        for entry in frame.into_iter().rev() {
            match entry.previous {
                Some(prev) => {
                    self.store.insert(entry.name, prev);
                }
                None => {
                    self.store.remove(&entry.name);
                }
            }
        }
    }

    /// Bind a new `let` variable in the current block; returns its SSA
    /// version (0 for a genuinely fresh name, otherwise high-water + 1).
    pub fn declare(&mut self, name: &str, ty: Type) -> u32 {
        let version = self.versions.get(name).copied().map(|v| v + 1).unwrap_or(0);
        self.versions.insert(name.to_string(), version);
        let previous = self.store.insert(
            name.to_string(),
            Versioned { ty, version },
        );
        if let Some(frame) = self.scope_stack.last_mut() {
            frame.push(ScopeEntry {
                name: name.to_string(),
                previous,
            });
        }
        version
    }

    /// Assignment: advance the SSA version of an existing variable.
    pub fn assign_version(&mut self, name: &str) -> u32 {
        let next = self.versions.get(name).copied().unwrap_or(0) + 1;
        self.versions.insert(name.to_string(), next);
        let ty = self.store.get(name).expect("type-checked assignment").ty;
        self.store.insert(
            name.to_string(),
            Versioned {
                ty,
                version: next,
            },
        );
        next
    }

    pub fn current(&self, name: &str) -> Option<(u32, Type)> {
        self.store.get(name).map(|v| (v.version, v.ty))
    }

    /// Record an SSA binding's native term and return its canonical key.
    pub fn bind_native(&mut self, name: &str, version: u32, term: NInt) {
        self.native_ssa.insert(ssa_key(name, version), term);
    }

    pub fn add_constraint(&mut self, z3: Bool<'ctx>, native: NBool) {
        self.pc_z3.push(z3);
        self.pc_native.push(native);
    }
}
