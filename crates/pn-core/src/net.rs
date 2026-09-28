//! Typed net model and structural construction.

use std::collections::HashMap;
use thiserror::Error;

/// Number of tokens on a place. Unsigned: token counts are never negative.
pub type Token = u64;

/// A place definition supplied when building a [`Net`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlaceDef {
    pub name: String,
    /// Explicit upper bound on the token count. `0` means the place must
    /// always be empty. Capacity is a hard modelling constraint, never a
    /// truncation threshold.
    pub capacity: Token,
}

/// A weighted arc definition.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArcDef {
    pub place: String,
    /// Positive arc weight. Multiple arcs between the same transition and
    /// place are rejected at construction; use the weight to express
    /// multiplicity.
    pub weight: Token,
}

/// A transition definition supplied when building a [`Net`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransitionDef {
    pub name: String,
    pub inputs: Vec<ArcDef>,
    pub outputs: Vec<ArcDef>,
}

/// Structural errors rejected at net construction.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum CoreError {
    #[error("the net has no places")]
    EmptyPlaceSet,
    #[error("duplicate place name: {0}")]
    DuplicatePlace(String),
    #[error("duplicate transition name: {0}")]
    DuplicateTransition(String),
    #[error("transition {transition}: {kind} arc references unknown place '{place}'")]
    UnknownPlace {
        transition: String,
        kind: &'static str,
        place: String,
    },
    #[error("transition {transition}: {kind} arc to '{place}' has weight 0")]
    ZeroWeight {
        transition: String,
        kind: &'static str,
        place: String,
    },
    #[error("transition {0}: lists place '{1}' more than once among its inputs")]
    DuplicateInput(String, String),
    #[error("transition {0}: lists place '{1}' more than once among its outputs")]
    DuplicateOutput(String, String),
    #[error("initial marking length {found} does not match place count {expected}")]
    BadInitialLength { found: usize, expected: usize },
    #[error("initial marking of place '{place}' is {tokens}, exceeding capacity {capacity}")]
    InitialExceedsCapacity {
        place: String,
        tokens: Token,
        capacity: Token,
    },
}

/// A validated, ordinary weighted place/transition Petri net with an explicit
/// finite capacity per place.
#[derive(Debug, Clone)]
pub struct Net {
    places: Vec<PlaceDef>,
    transitions: Vec<TransitionDef>,
    place_index: HashMap<String, usize>,
    transition_index: HashMap<String, usize>,
    initial: Vec<Token>,
}

impl Net {
    /// Validate and build a net. Fails on duplicate names, dangling arc
    /// references, non-positive weights or an initial marking outside the
    /// declared capacities.
    pub fn new(
        places: Vec<PlaceDef>,
        transitions: Vec<TransitionDef>,
        initial: Vec<Token>,
    ) -> Result<Net, CoreError> {
        if places.is_empty() {
            return Err(CoreError::EmptyPlaceSet);
        }

        let mut place_index = HashMap::with_capacity(places.len());
        for (i, p) in places.iter().enumerate() {
            if place_index.insert(p.name.clone(), i).is_some() {
                return Err(CoreError::DuplicatePlace(p.name.clone()));
            }
        }

        let mut transition_index = HashMap::with_capacity(transitions.len());
        for (i, t) in transitions.iter().enumerate() {
            if transition_index.insert(t.name.clone(), i).is_some() {
                return Err(CoreError::DuplicateTransition(t.name.clone()));
            }
            let mut seen_in = HashMap::new();
            for a in &t.inputs {
                if !place_index.contains_key(&a.place) {
                    return Err(CoreError::UnknownPlace {
                        transition: t.name.clone(),
                        kind: "input",
                        place: a.place.clone(),
                    });
                }
                if a.weight == 0 {
                    return Err(CoreError::ZeroWeight {
                        transition: t.name.clone(),
                        kind: "input",
                        place: a.place.clone(),
                    });
                }
                if seen_in.insert(a.place.clone(), ()).is_some() {
                    return Err(CoreError::DuplicateInput(t.name.clone(), a.place.clone()));
                }
            }
            let mut seen_out = HashMap::new();
            for a in &t.outputs {
                if !place_index.contains_key(&a.place) {
                    return Err(CoreError::UnknownPlace {
                        transition: t.name.clone(),
                        kind: "output",
                        place: a.place.clone(),
                    });
                }
                if a.weight == 0 {
                    return Err(CoreError::ZeroWeight {
                        transition: t.name.clone(),
                        kind: "output",
                        place: a.place.clone(),
                    });
                }
                if seen_out.insert(a.place.clone(), ()).is_some() {
                    return Err(CoreError::DuplicateOutput(t.name.clone(), a.place.clone()));
                }
            }
        }

        if initial.len() != places.len() {
            return Err(CoreError::BadInitialLength {
                found: initial.len(),
                expected: places.len(),
            });
        }
        for (p, &tokens) in places.iter().zip(initial.iter()) {
            if tokens > p.capacity {
                return Err(CoreError::InitialExceedsCapacity {
                    place: p.name.clone(),
                    tokens,
                    capacity: p.capacity,
                });
            }
        }

        Ok(Net {
            places,
            transitions,
            place_index,
            transition_index,
            initial,
        })
    }

    pub fn place_count(&self) -> usize {
        self.places.len()
    }

    pub fn transition_count(&self) -> usize {
        self.transitions.len()
    }

    pub fn place_name(&self, index: usize) -> &str {
        &self.places[index].name
    }

    pub fn transition_name(&self, index: usize) -> &str {
        &self.transitions[index].name
    }

    pub fn capacity(&self, index: usize) -> Token {
        self.places[index].capacity
    }

    pub fn place_index(&self, name: &str) -> Option<usize> {
        self.place_index.get(name).copied()
    }

    pub fn transition_index(&self, name: &str) -> Option<usize> {
        self.transition_index.get(name).copied()
    }

    pub fn places(&self) -> &[PlaceDef] {
        &self.places
    }

    pub fn transitions(&self) -> &[TransitionDef] {
        &self.transitions
    }

    pub fn initial(&self) -> &[Token] {
        &self.initial
    }
}
