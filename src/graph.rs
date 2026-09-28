//! Graph construction for the difference-constraint system.
//!
//! # Reduction
//!
//! A constraint `x - y <= c` is equivalent to a weighted directed edge
//! `y -> x` with weight `c`: the standard potential inequality
//! `dist[x] <= dist[y] + w(y,x)`. A system is feasible iff this digraph has
//! no negative-weight cycle. Cycles correspond to chains of inequalities
//! summed around a loop, which are exactly the unsat proofs.
//!
//! The graph may be disconnected. Bellman–Ford is initialized with distance 0
//! at *every* vertex (equivalent to a super-source with 0-weight edges to all
//! vertices), so every connected component is explored without any special
//! handling. Component information is still computed and exposed, both as an
//! explicit correctness/test aid and on the solve response.

use std::collections::BTreeMap;

use serde::Serialize;

use crate::error::ServiceResult;
use crate::model::Constraint;

/// A directed weighted edge. One constraint produces exactly one edge; the
/// edge remembers its originating constraint id so that conflict evidence can
/// be reported with *original constraint ids only* — no anonymous edges ever
/// escape the kernel.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Edge {
    pub from: usize,
    pub to: usize,
    pub weight: i64,
    pub constraint_id: String,
}

/// Union-find used to label undirected connected components.
#[derive(Debug, Clone)]
struct Dsu {
    parent: Vec<usize>,
    rank: Vec<u8>,
}

impl Dsu {
    fn new(n: usize) -> Self {
        Self {
            parent: (0..n).collect(),
            rank: vec![0; n],
        }
    }
    fn find(&mut self, mut x: usize) -> usize {
        while self.parent[x] != x {
            self.parent[x] = self.parent[self.parent[x]];
            x = self.parent[x];
        }
        x
    }
    fn union(&mut self, a: usize, b: usize) {
        let (ra, rb) = (self.find(a), self.find(b));
        if ra == rb {
            return;
        }
        match self.rank[ra].cmp(&self.rank[rb]) {
            std::cmp::Ordering::Less => self.parent[ra] = rb,
            std::cmp::Ordering::Greater => self.parent[rb] = ra,
            std::cmp::Ordering::Equal => {
                self.parent[rb] = ra;
                self.rank[ra] += 1;
            }
        }
    }
}

/// Built graph: vertices are interned variables, edges carry constraint ids.
#[derive(Debug, Clone)]
pub struct Graph {
    /// Sorted by first appearance; index in this vec is the vertex number.
    pub variables: Vec<String>,
    pub edges: Vec<Edge>,
    /// Vertex -> connected-component id (ids are canonical root indices,
    /// renumbered contiguously in [`Graph::component_of`]).
    component_of: Vec<usize>,
    pub component_count: usize,
}

/// Serializable component summary returned to clients and logged in tests.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ComponentInfo {
    pub id: usize,
    pub vertices: Vec<String>,
    pub edges: usize,
}

impl Graph {
    /// Intern every variable and emit one edge per constraint.
    /// `constraints` must already be validated (ids unique — the store
    /// guarantees this); edges keep insertion order.
    pub fn build(constraints: &[Constraint]) -> ServiceResult<Self> {
        let mut index: BTreeMap<String, usize> = BTreeMap::new();
        let mut variables: Vec<String> = Vec::new();
        let intern = |name: &str,
                          index: &mut BTreeMap<String, usize>,
                          variables: &mut Vec<String>|
         -> usize {
            if let Some(&i) = index.get(name) {
                return i;
            }
            let i = variables.len();
            index.insert(name.to_string(), i);
            variables.push(name.to_string());
            i
        };

        let mut edges = Vec::with_capacity(constraints.len());
        for c in constraints {
            let y = intern(&c.rhs, &mut index, &mut variables);
            let x = intern(&c.lhs, &mut index, &mut variables);
            edges.push(Edge {
                from: y,
                to: x,
                weight: c.bound,
                constraint_id: c.id.clone(),
            });
        }

        // Undirected components over every edge.
        let mut dsu = Dsu::new(variables.len());
        for e in &edges {
            dsu.union(e.from, e.to);
        }
        // Renumber roots to 0..k in vertex order for stable component ids.
        let mut root_to_component: BTreeMap<usize, usize> = BTreeMap::new();
        let mut component_of = Vec::with_capacity(variables.len());
        for v in 0..variables.len() {
            let root = dsu.find(v);
            let next = root_to_component.len();
            let cid = *root_to_component.entry(root).or_insert(next);
            component_of.push(cid);
        }
        let component_count = root_to_component.len();

        Ok(Self {
            variables,
            edges,
            component_of,
            component_count,
        })
    }

    pub fn vertex_count(&self) -> usize {
        self.variables.len()
    }

    pub fn component(&self, vertex: usize) -> usize {
        self.component_of[vertex]
    }

    /// Human/log-friendly component breakdown.
    pub fn component_info(&self) -> Vec<ComponentInfo> {
        let mut out: Vec<ComponentInfo> = (0..self.component_count)
            .map(|id| ComponentInfo {
                id,
                vertices: Vec::new(),
                edges: 0,
            })
            .collect();
        for (v, name) in self.variables.iter().enumerate() {
            out[self.component_of[v]].vertices.push(name.clone());
        }
        for e in &self.edges {
            out[self.component_of[e.from]].edges += 1;
        }
        out
    }

    /// Test-only constructor: build a single-vertex graph with `edge_count`
    /// zero-weight self loops, to exercise kernel resource guards without
    /// going through the store.
    #[cfg(test)]
    pub fn self_loops_for_test(edge_count: usize) -> Self {
        let edges: Vec<Edge> = (0..edge_count)
            .map(|i| Edge {
                from: 0,
                to: 0,
                weight: 0,
                constraint_id: format!("e{i}"),
            })
            .collect();
        Self {
            variables: vec!["x".to_string()],
            edges,
            component_of: vec![0],
            component_count: 1,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn c(id: &str, x: &str, y: &str, w: i64) -> Constraint {
        Constraint::new(id, x, y, w).unwrap()
    }

    #[test]
    fn edge_direction_is_rhs_to_lhs() {
        let g = Graph::build(&[c("c1", "x", "y", 5)]).unwrap();
        assert_eq!(g.variables, vec!["y", "x"]);
        assert_eq!(
            g.edges[0],
            Edge {
                from: 0,
                to: 1,
                weight: 5,
                constraint_id: "c1".into()
            }
        );
    }

    #[test]
    fn disconnected_components_are_labelled() {
        // a-b component, d-e component, and c appears only once (isolated vertex).
        let cs = vec![
            c("c1", "a", "b", 1),
            c("c2", "b", "a", 1),
            c("c3", "d", "e", 2),
            c("c4", "c", "c", 0),
        ];
        let g = Graph::build(&cs).unwrap();
        assert_eq!(g.component_count, 3);
        let info = g.component_info();
        let sizes: Vec<usize> = info.iter().map(|i| i.vertices.len()).collect();
        assert!(sizes.contains(&2));
        assert!(sizes.contains(&1));
    }
}
