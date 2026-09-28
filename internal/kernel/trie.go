// Package kernel is the pure compute core: an immutable copy-on-write
// subscription trie with bounded wildcard matching. It knows nothing about
// HTTP, SQL or concurrency control — the router owns snapshot publication,
// which is what makes "updates use subscription snapshots, messages bind to a
// routing version" enforceable.
//
// Semantics implemented here (see internal/topic for the authoritative rules):
//
//   - "+" follows the single-level edge and matches exactly one layer,
//     including an empty layer.
//   - "#" never descends: a filter ".../#" attaches the subscriber to the node
//     reached by its prefix, and that node matches zero or more remaining
//     topic layers.
//   - Empty filter layers are ordinary literal edges keyed by "", so the
//     structural distinctions "/a" vs "a" vs "a/" are preserved.
//   - A subscriber reachable through several conceptual paths is collected
//     exactly once; DedupHits reports how often the guard fired.
package kernel

import "sort"

// node is immutable after Build publishes it. Builders mutate only clones.
type node struct {
	// subs holds subscribers whose filter terminates exactly here
	// (literal or "+" terminus).
	subs map[string]struct{}
	// multi holds subscribers whose filter terminates with "#" here;
	// it matches this node with zero or more remaining layers.
	multi map[string]struct{}
	// plus is the single-level wildcard child.
	plus *node
	// children maps literal layer text (including "") to a child node.
	children map[string]*node
}

func newNode() *node {
	return &node{}
}

func (n *node) shallowClone() *node {
	c := &node{plus: n.plus}
	if len(n.subs) > 0 {
		c.subs = make(map[string]struct{}, len(n.subs))
		for k := range n.subs {
			c.subs[k] = struct{}{}
		}
	}
	if len(n.multi) > 0 {
		c.multi = make(map[string]struct{}, len(n.multi))
		for k := range n.multi {
			c.multi[k] = struct{}{}
		}
	}
	if len(n.children) > 0 {
		c.children = make(map[string]*node, len(n.children))
		for k, v := range n.children {
			c.children[k] = v
		}
	}
	return c
}

func (n *node) isEmpty() bool {
	return n.plus == nil && len(n.subs) == 0 && len(n.multi) == 0 && len(n.children) == 0
}

// Snapshot is one immutable routing-table version. Match is safe for
// concurrent use without locks.
type Snapshot struct {
	root    *node
	Version int64
}

// Stats counts index work performed by one Match. The performance tests assert
// on these counters to prove matching walks the index instead of scanning the
// subscription table.
type Stats struct {
	// NodeVisits is the number of trie nodes dequeued and inspected.
	NodeVisits int
	// EdgeLookups is the number of child edges probed (literal and "+").
	EdgeLookups int
	// TerminalsCollected is the number of terminal sets reached ("#" nodes
	// and exact-depth terminals), before de-duplication.
	TerminalsCollected int
	// DedupHits is the number of duplicate subscriber observations discarded.
	// Must stay zero on the current structure; the guard and counter exist so a
	// structural regression that creates duplicate delivery paths fails tests.
	DedupHits int
}

// MatchResult is one routing decision's raw output.
type MatchResult struct {
	Subscribers []string
	Stats       Stats
}

// edge describes how a child hangs off its parent, for copy-on-write descent
// and post-delete pruning.
type edgeKind int

const (
	edgeLiteral edgeKind = iota
	edgePlus
)

type edgeRef struct {
	parent *node
	kind   edgeKind
	key    string // literal key when kind == edgeLiteral
}

// Builder produces a new Snapshot from a base snapshot, mutating only cloned
// nodes. All snapshots share their unchanged subtrees across versions, which
// is what keeps historical versions cheap to retain for replay.
type Builder struct {
	base   *Snapshot
	root   *node
	clones map[*node]*node
}

// NewBuilder starts a new version on top of base (nil means empty).
func NewBuilder(base *Snapshot) *Builder {
	var oldRoot *node
	if base != nil {
		oldRoot = base.root
	}
	if oldRoot == nil {
		oldRoot = newNode()
	}
	b := &Builder{
		base:   base,
		clones: make(map[*node]*node),
	}
	b.root = b.mutable(oldRoot)
	return b
}

// mutable returns a writable copy of n, memoized per builder so a shared
// prefix is cloned once even when many operations touch it.
func (b *Builder) mutable(n *node) *node {
	if c, ok := b.clones[n]; ok {
		return c
	}
	c := n.shallowClone()
	b.clones[n] = c
	return c
}

// descend follows one layer from cur, cloning the child on first touch.
func (b *Builder) descend(cur *node, layer string) *node {
	if layer == "+" {
		child := cur.plus
		if child == nil {
			cur.plus = newNode()
			return cur.plus
		}
		c := b.mutable(child)
		cur.plus = c
		return c
	}
	child := cur.children[layer]
	if child == nil {
		if cur.children == nil {
			cur.children = make(map[string]*node)
		}
		c := newNode()
		cur.children[layer] = c
		return c
	}
	c := b.mutable(child)
	cur.children[layer] = c
	return c
}

// Insert adds subscriber for the given validated filter layers.
func (b *Builder) Insert(layers []string, subscriber string) {
	cur := b.root
	last := len(layers) - 1
	for i, layer := range layers {
		if layer == "#" {
			// "#" is only legal at the terminus; it attaches in place.
			if cur.multi == nil {
				cur.multi = make(map[string]struct{})
			}
			cur.multi[subscriber] = struct{}{}
			return
		}
		if i == last {
			cur = b.descend(cur, layer)
			if cur.subs == nil {
				cur.subs = make(map[string]struct{})
			}
			cur.subs[subscriber] = struct{}{}
			return
		}
		cur = b.descend(cur, layer)
	}
	// Empty filter path (layers is nil): the subscriber sits on the root.
	if cur.subs == nil {
		cur.subs = make(map[string]struct{})
	}
	cur.subs[subscriber] = struct{}{}
}

// Delete removes subscriber from the given validated filter layers and prunes
// emptied branches. Deleting something absent is a no-op. It never touches
// terminals belonging to other filters, so deleting one subscription cannot
// change another subscriber's routing — historical snapshots are untouched
// regardless.
func (b *Builder) Delete(layers []string, subscriber string) bool {
	cur := b.root
	path := make([]edgeRef, 0, len(layers))

	// Walk to the terminal node, recording the edge chain.
	for i, layer := range layers {
		if layer == "#" {
			if i != len(layers)-1 {
				return false // validated filters never hit this; defensive
			}
			if _, ok := cur.multi[subscriber]; !ok {
				return false
			}
			delete(cur.multi, subscriber)
			b.prune(path)
			return true
		}
		ref := edgeRef{parent: cur}
		var next *node
		if layer == "+" {
			ref.kind = edgePlus
			next = cur.plus
		} else {
			ref.kind = edgeLiteral
			ref.key = layer
			next = cur.children[layer]
		}
		if next == nil {
			return false
		}
		next = b.mutable(next)
		b.reattach(ref, next)
		path = append(path, ref)
		cur = next
	}
	if _, ok := cur.subs[subscriber]; !ok {
		return false
	}
	delete(cur.subs, subscriber)
	b.prune(path)
	return true
}

// reattach points the (already mutable) parent edge at the cloned child.
func (b *Builder) reattach(ref edgeRef, child *node) {
	switch ref.kind {
	case edgePlus:
		ref.parent.plus = child
	case edgeLiteral:
		ref.parent.children[ref.key] = child
	}
}

// prune removes empty nodes from the deepest back up to (but not including)
// the root.
func (b *Builder) prune(path []edgeRef) {
	for i := len(path) - 1; i >= 0; i-- {
		ref := path[i]
		var child *node
		switch ref.kind {
		case edgePlus:
			child = ref.parent.plus
		case edgeLiteral:
			child = ref.parent.children[ref.key]
		}
		if child == nil || !child.isEmpty() {
			return
		}
		switch ref.kind {
		case edgePlus:
			ref.parent.plus = nil
		case edgeLiteral:
			delete(ref.parent.children, ref.key)
		}
	}
}

// Build publishes the immutable snapshot at the given version.
func (b *Builder) Build(version int64) *Snapshot {
	return &Snapshot{root: b.root, Version: version}
}

// Match routes topicLayers against the snapshot.
func (s *Snapshot) Match(topicLayers []string) MatchResult {
	if s == nil || s.root == nil {
		return MatchResult{Subscribers: []string{}}
	}
	var st Stats
	found := make(map[string]struct{})

	type frame struct {
		n     *node
		depth int
	}
	// Max active frames is bounded by the number of branches at each depth
	// (literal + "+"), so the stack is tiny; depth never exceeds len(layers)+1.
	stack := []frame{{n: s.root, depth: 0}}
	for len(stack) > 0 {
		f := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		st.NodeVisits++

		// "#" at this node matches zero or more remaining layers, always.
		if len(f.n.multi) > 0 {
			st.TerminalsCollected++
			for sub := range f.n.multi {
				if _, dup := found[sub]; dup {
					st.DedupHits++
				}
				found[sub] = struct{}{}
			}
		}

		if f.depth == len(topicLayers) {
			// Exact-depth terminus (literal/"+" filter ending here).
			if len(f.n.subs) > 0 {
				st.TerminalsCollected++
				for sub := range f.n.subs {
					if _, dup := found[sub]; dup {
						st.DedupHits++
					}
					found[sub] = struct{}{}
				}
			}
			continue
		}

		layer := topicLayers[f.depth]
		// Literal edge — also covers empty filter layers keyed by "".
		st.EdgeLookups++
		if c := f.n.children[layer]; c != nil {
			stack = append(stack, frame{n: c, depth: f.depth + 1})
		}
		// Single-level edge; "+" matches an empty layer just like any other.
		st.EdgeLookups++
		if f.n.plus != nil {
			stack = append(stack, frame{n: f.n.plus, depth: f.depth + 1})
		}
	}

	subs := make([]string, 0, len(found))
	for sub := range found {
		subs = append(subs, sub)
	}
	sort.Strings(subs)
	return MatchResult{Subscribers: subs, Stats: st}
}
