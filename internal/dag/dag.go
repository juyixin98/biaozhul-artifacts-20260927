// Package dag builds and validates the dependency graph of desired resources.
// Create order is a deterministic topological order; delete order is its
// exact reverse. Cycles and dangling references are compute failures.
package dag

import (
	"fmt"
	"sort"

	"infraplanner/internal/errorsx"
	"infraplanner/internal/model"
)

// Graph maps resource id -> dependency ids (edges point from a resource to the
// resources that must exist BEFORE it, i.e. its parents).
type Graph struct {
	ids    []string
	deps   map[string][]string
	kinds  map[string]model.Kind
}

// Build constructs a graph from specs, validating that every DependsOn target
// exists. Cross-kind references are allowed (a subnet may depend on a network).
func Build(specs []model.Spec) (*Graph, error) {
	g := &Graph{
		deps:  make(map[string][]string, len(specs)),
		kinds: make(map[string]model.Kind, len(specs)),
	}
	known := make(map[string]bool, len(specs))
	for _, s := range specs {
		if known[s.ID] {
			return nil, errorsx.Compute("DUP_ID", "duplicate resource id",
				map[string]any{"id": s.ID})
		}
		known[s.ID] = true
		g.kinds[s.ID] = s.Kind
	}
	for _, s := range specs {
		seen := map[string]bool{}
		for _, dep := range s.DependsOn {
			if !known[dep] {
				return nil, errorsx.Compute("DANGLING_REF",
					fmt.Sprintf("resource %q depends on unknown resource %q", s.ID, dep),
					map[string]any{"id": s.ID, "missing_ref": dep})
			}
			if dep == s.ID {
				return nil, errorsx.Compute("SELF_REF", "resource depends on itself",
					map[string]any{"id": s.ID})
			}
			if seen[dep] {
				continue
			}
			seen[dep] = true
			g.deps[s.ID] = append(g.deps[s.ID], dep)
		}
		g.ids = append(g.ids, s.ID)
	}
	return g, nil
}

// CreateOrder returns a deterministic topological order: parents before
// children. Ties break lexicographically so plans are reproducible.
func (g *Graph) CreateOrder() ([]string, error) {
	indeg := make(map[string]int, len(g.ids))
	children := make(map[string][]string, len(g.ids))
	for _, id := range g.ids {
		indeg[id] = 0
	}
	for id, ds := range g.deps {
		indeg[id] = len(ds)
		for _, p := range ds {
			children[p] = append(children[p], id)
		}
	}
	ready := make([]string, 0)
	for _, id := range g.ids {
		if indeg[id] == 0 {
			ready = append(ready, id)
		}
	}
	sort.Strings(ready)

	order := make([]string, 0, len(g.ids))
	for len(ready) > 0 {
		// pop smallest
		cur := ready[0]
		ready = ready[1:]
		order = append(order, cur)
		ch := append([]string(nil), children[cur]...)
		sort.Strings(ch)
		for _, c := range ch {
			indeg[c]--
			if indeg[c] == 0 {
				ready = append(ready, c)
				sort.Strings(ready)
			}
		}
	}
	if len(order) != len(g.ids) {
		cycle := g.findCycle()
		return nil, errorsx.Compute("DEPENDENCY_CYCLE",
			"dependency graph contains a cycle",
			map[string]any{"cycle": cycle})
	}
	return order, nil
}

// DeleteOrder returns the destruction order: exact reverse of create order,
// i.e. dependents are removed before their dependencies.
func (g *Graph) DeleteOrder() ([]string, error) {
	ord, err := g.CreateOrder()
	if err != nil {
		return nil, err
	}
	out := make([]string, len(ord))
	for i := range ord {
		out[len(ord)-1-i] = ord[i]
	}
	return out, nil
}

// SubsetDeleteOrder returns a reverse-topological order over only the given
// ids (used for destroys within a mixed plan). Edges to surviving resources are
// ignored; edges among the destroyed set still enforce dependent-first.
func (g *Graph) SubsetDeleteOrder(ids []string) ([]string, error) {
	want := make(map[string]bool, len(ids))
	for _, id := range ids {
		want[id] = true
	}
	sub := make(map[string][]string)
	// Reverse edges: child -> parents. For deletion, a child must be deleted
	// before its parents; compute ordering over the induced subgraph.
	indeg := map[string]int{} // number of in-set dependents blocking delete
	parents := map[string][]string{}
	for id := range want {
		indeg[id] = 0
	}
	for id := range want {
		for _, p := range g.deps[id] {
			if want[p] {
				// id is a dependent of p; p cannot be deleted until id is gone.
				parents[id] = append(parents[id], p)
				indeg[p]++
			}
		}
	}
	ready := make([]string, 0)
	for id := range want {
		if indeg[id] == 0 {
			ready = append(ready, id)
		}
	}
	sort.Strings(ready)
	out := make([]string, 0, len(want))
	for len(ready) > 0 {
		cur := ready[0]
		ready = ready[1:]
		out = append(out, cur)
		for _, p := range parents[cur] {
			indeg[p]--
			if indeg[p] == 0 {
				ready = append(ready, p)
				sort.Strings(ready)
			}
		}
	}
	if len(out) != len(want) {
		return nil, errorsx.Compute("DELETE_ORDER_CYCLE",
			"could not order deletions", nil)
	}
	return out, nil
}

// DepsOf returns the declared dependencies of a resource.
func (g *Graph) DepsOf(id string) []string { return g.deps[id] }

func (g *Graph) findCycle() []string {
	color := map[string]int{} // 0 white,1 gray,2 black
	var stack, cyc []string
	var dfs func(string) bool
	dfs = func(u string) bool {
		color[u] = 1
		stack = append(stack, u)
		ds := append([]string(nil), g.deps[u]...)
		sort.Strings(ds)
		for _, v := range ds {
			if color[v] == 0 {
				if dfs(v) {
					return true
				}
			} else if color[v] == 1 {
				for i, x := range stack {
					if x == v {
						cyc = append([]string(nil), stack[i:]...)
						cyc = append(cyc, v)
						return true
					}
				}
			}
		}
		color[u] = 2
		stack = stack[:len(stack)-1]
		return false
	}
	ids := append([]string(nil), g.ids...)
	sort.Strings(ids)
	for _, id := range ids {
		if color[id] == 0 && dfs(id) {
			return cyc
		}
	}
	return nil
}
