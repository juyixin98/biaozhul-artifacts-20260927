//! Complemented edges (complement-pointer representation).
//!
//! An [`Edge`] packs a **stable logical node id** with one parity bit. Logical
//! ids are independent of the arena slot a node currently occupies: garbage
//! collection compacts slots but keeps logical ids fixed, so an outstanding
//! reference to a node that survives collection keeps working (the manager
//! redirects its id to the new slot), while a reference to a collected id is
//! detected as stale rather than misread.
//!
//! Terminal constants are the two edges over one terminal id:
//!
//! * `Edge::FALSE` — logical id 1, even
//! * `Edge::TRUE`  — logical id 1, odd
//!
//! Canonicalization invariant: for every internal node we store, its low edge
//! is *uncomplemented*. Negation is a free parity flip, and each Boolean
//! function has exactly one edge encoding, which is what makes structural
//! comparison a sound equivalence test.

use serde::{Deserialize, Serialize};

/// One reference to a BDD node, possibly complemented.
///
/// Internal `u32` layout:
///
/// ```text
/// bits 1..=31 : stable logical node id (id 1 is the single terminal)
/// bit  0      : complement parity
/// ```
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Edge(u32);

const COMP_BIT: u32 = 1;

/// Stable logical id of the single terminal.
pub(crate) const TERMINAL_ID: u32 = 1;

impl Edge {
    /// The constant `false` terminal edge.
    pub const FALSE: Edge = Edge(TERMINAL_ID << 1);
    /// The constant `true` terminal edge.
    pub const TRUE: Edge = Edge((TERMINAL_ID << 1) | COMP_BIT);

    /// Construct an edge from a stable logical id and complement bit.
    #[inline]
    pub(crate) fn new(id: u32, comp: bool) -> Edge {
        Edge((id << 1) | u32::from(comp))
    }

    /// Stable logical node id.
    #[inline]
    pub(crate) fn id(self) -> u32 {
        self.0 >> 1
    }

    /// Complement parity of this edge.
    #[inline]
    pub(crate) fn comp(self) -> bool {
        (self.0 & COMP_BIT) == COMP_BIT
    }

    /// Raw packed encoding (used in [`crate::kernel::NodeRef`] and diagnostics).
    #[inline]
    pub fn raw(self) -> u32 {
        self.0
    }

    /// Reconstruct from [`raw`](Self::raw); validated against the manager.
    #[inline]
    pub(crate) fn from_raw(raw: u32) -> Edge {
        Edge(raw)
    }

    /// Logical negation: flip the complement bit.
    #[inline]
    pub fn negate(self) -> Edge {
        Edge(self.0 ^ COMP_BIT)
    }

    /// Is this an edge to the terminal logical id?
    #[inline]
    pub(crate) fn is_terminal(self) -> bool {
        self.id() == TERMINAL_ID
    }

    /// Boolean value of a terminal edge.
    #[inline]
    pub(crate) fn terminal_value(self) -> bool {
        debug_assert!(self.is_terminal());
        self.comp()
    }
}

impl std::fmt::Debug for Edge {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if self.is_terminal() {
            write!(f, "{}", self.comp())
        } else {
            write!(f, "v{}", self.id())?;
            if self.comp() {
                f.write_str("¬")?;
            }
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constants_and_negation() {
        assert!(Edge::FALSE.is_terminal());
        assert!(Edge::TRUE.is_terminal());
        assert!(!Edge::FALSE.terminal_value());
        assert!(Edge::TRUE.terminal_value());
        assert_eq!(Edge::FALSE.negate(), Edge::TRUE);
        assert_eq!(Edge::TRUE.negate(), Edge::FALSE);
        assert_eq!(Edge::FALSE.negate().negate(), Edge::FALSE);
    }

    #[test]
    fn internal_roundtrip() {
        let e = Edge::new(7, true);
        assert_eq!(e.id(), 7);
        assert!(e.comp());
        assert!(!e.is_terminal());
        assert_eq!(Edge::from_raw(e.raw()), e);
    }
}
