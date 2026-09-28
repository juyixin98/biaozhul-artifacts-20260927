package core

import (
	"sort"

	"lifecycle.local/v1/internal/model"
)

// BlockingGraph is the directed graph that can deadlock a foreground
// deletion: a foreground-deleting resource u waits for every *blocking*
// dependent v that still exists and is itself foreground-deleting.
// Edge u -> v means "u cannot be physically deleted until v is gone".
type BlockingGraph struct {
	// Out[u] lists blocker dependents of u (only foreground-deleting
	// nodes appear).
	Out map[string][]string
}

// BuildBlockingGraph constructs the blocking graph for the given live
// set. Only resources that currently carry the foreground finalizer and
// a deletionTimestamp participate; edges come exclusively from
// BlockOwnerDeletion references (the flag's ordering relative to the
// propagation policy is the whole reason a non-blocking dependent does
// NOT hold its foreground owner).
func BuildBlockingGraph(idx *LiveIndex) *BlockingGraph {
	g := &BlockingGraph{Out: map[string][]string{}}
	for _, r := range idx.ByUID {
		if !isForegroundDeleting(r) {
			continue
		}
		g.Out[r.UID] = nil
	}
	// v blocks u when v references u with block=true.
	for _, v := range idx.ByUID {
		if !isForegroundDeleting(v) {
			continue
		}
		for _, ref := range v.OwnerRefs {
			if !ref.BlockOwnerDeletion {
				continue
			}
			if _, ok := g.Out[ref.UID]; ok {
				g.Out[ref.UID] = append(g.Out[ref.UID], v.UID)
			}
		}
	}
	for k := range g.Out {
		sort.Strings(g.Out[k])
	}
	return g
}

func isForegroundDeleting(r *model.Resource) bool {
	return r.IsDeleting() && r.Policy() == model.PolicyForeground &&
		r.HasFinalizer(model.FinalizerDeletionCohort)
}

// Cycle is one diagnosed closed chain of blocking edges.
type Cycle struct {
	// UIDs are ordered along the blocking direction; the chain closes
	// back to UIDs[0].
	UIDs []string
}

// FindCycles returns every distinct cycle (SCC of size >= 2, plus self
// loops). It uses Tarjan SCC so a node shared by two diamonds is not
// misreported, and normalises each cycle to a lexicographically
// smallest-UID rotation for stable logs.
func (g *BlockingGraph) FindCycles() []Cycle {
	tarjan := newTarjan(g.Out)
	sccs := tarjan.run()
	var cycles []Cycle
	for _, scc := range sccs {
		if len(scc) == 1 {
			u := scc[0]
			if contains(g.Out[u], u) {
				cycles = append(cycles, Cycle{UIDs: []string{u}})
			}
			continue
		}
		cyc := g.orderCycle(scc)
		cycles = append(cycles, Cycle{UIDs: cyc})
	}
	sort.Slice(cycles, func(i, j int) bool {
		return cycles[i].UIDs[0] < cycles[j].UIDs[0]
	})
	return cycles
}

// orderCycle orders an SCC's nodes into one closed blocking chain by
// repeatedly following the smallest neighbour inside the SCC. The
// result starts at the lexicographically smallest UID.
func (g *BlockingGraph) orderCycle(nodes []string) []string {
	in := map[string]bool{}
	for _, n := range nodes {
		in[n] = true
	}
	start := minString(nodes)
	out := []string{start}
	visited := map[string]bool{start: true}
	cur := start
	for len(out) < len(nodes) {
		var next string
		for _, nb := range g.Out[cur] {
			if in[nb] && !visited[nb] {
				if next == "" || nb < next {
					next = nb
				}
			}
		}
		if next == "" {
			// SCC with branching: fall back to sorted remaining nodes
			// so the diagnosis still lists every participant.
			rem := make([]string, 0, len(nodes)-len(out))
			for _, n := range nodes {
				if !visited[n] {
					rem = append(rem, n)
				}
			}
			sort.Strings(rem)
			out = append(out, rem...)
			break
		}
		out = append(out, next)
		visited[next] = true
		cur = next
	}
	return out
}

// BreakUID deterministically chooses the cycle node at which the
// foreground wait is removed: the lexicographically smallest UID across
// ALL detected cycles. Determinism is mandatory: the same illegal input
// must produce the same diagnostic and the same break on every run
// instead of random (or no) progress.
func BreakUID(cycles []Cycle) string {
	var best string
	for _, c := range cycles {
		for _, u := range c.UIDs {
			if best == "" || u < best {
				best = u
			}
		}
	}
	return best
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

func minString(xs []string) string {
	m := xs[0]
	for _, x := range xs[1:] {
		if x < m {
			m = x
		}
	}
	return m
}

// ---- Tarjan SCC ----

type tarjan struct {
	adj     map[string][]string
	index   map[string]int
	low     map[string]int
	onStack map[string]bool
	stack   []string
	next    int
	sccs    [][]string
}

func newTarjan(adj map[string][]string) *tarjan {
	return &tarjan{
		adj:     adj,
		index:   map[string]int{},
		low:     map[string]int{},
		onStack: map[string]bool{},
	}
}

func (t *tarjan) run() [][]string {
	vertices := make([]string, 0, len(t.adj))
	for v := range t.adj {
		vertices = append(vertices, v)
	}
	sort.Strings(vertices)
	for _, v := range vertices {
		if _, ok := t.index[v]; !ok {
			t.strongConnect(v)
		}
	}
	return t.sccs
}

func (t *tarjan) strongConnect(v string) {
	t.index[v] = t.next
	t.low[v] = t.next
	t.next++
	t.stack = append(t.stack, v)
	t.onStack[v] = true

	neighbours := append([]string{}, t.adj[v]...)
	sort.Strings(neighbours)
	for _, w := range neighbours {
		if _, ok := t.adj[w]; !ok {
			continue
		}
		if _, seen := t.index[w]; !seen {
			t.strongConnect(w)
			if t.low[w] < t.low[v] {
				t.low[v] = t.low[w]
			}
		} else if t.onStack[w] {
			if t.index[w] < t.low[v] {
				t.low[v] = t.index[w]
			}
		}
	}

	if t.low[v] == t.index[v] {
		var scc []string
		for {
			w := t.stack[len(t.stack)-1]
			t.stack = t.stack[:len(t.stack)-1]
			t.onStack[w] = false
			scc = append(scc, w)
			if w == v {
				break
			}
		}
		t.sccs = append(t.sccs, scc)
	}
}
