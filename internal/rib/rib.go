// Package rib implements the routing information base: per-address-family
// compressed prefix trees, versioned atomic snapshots, deterministic
// best-route selection and recursive next-hop resolution.
//
// Selection order for a destination is fixed:
//
//  1. longest matching prefix (an administrative distance can never make a
//     shorter prefix win over a longer one), then
//  2. among candidates at that same prefix: lower administrative distance,
//     then lower metric, then the candidate installed first (sequence).
//
// Mutations arrive as batches. A batch is validated against a copy of the
// current snapshot, persisted as one unit (when a store is attached), and
// only then swapped in, so every reader sees the complete batch at one new
// table version.
package rib

import (
	"encoding/json"
	"fmt"
	"sync"
	"sync/atomic"

	"github.com/opp221/ribd/internal/netmodel"
	"github.com/opp221/ribd/internal/trie"
)

// Status classifies a lookup outcome.
type Status string

const (
	// StatusResolved: a direct next hop (egress interface) was reached.
	StatusResolved Status = "resolved"
	// StatusIndeterminate: no decision could be made (no route, dangling
	// recursion). The request is neither accepted nor rejected.
	StatusIndeterminate Status = "indeterminate"
	// StatusRejected: a definite policy error in the graph (resolution loop,
	// depth limit exceeded) or invalid input.
	StatusRejected Status = "rejected"
)

// Failure gives a machine-readable reason for non-resolved outcomes.
type Failure string

const (
	FailureNoRoute       Failure = "no_route"
	FailureUnresolved    Failure = "unresolved"
	FailureLoop          Failure = "loop"
	FailureDepthExceeded Failure = "depth_exceeded"
	FailureCrossFamily   Failure = "cross_family"
	FailureBadQuery      Failure = "bad_query"
)

// Entry is one candidate installed at a prefix. Trie nodes hold one EntrySet.
type Entry struct {
	Route netmodel.Route
}

// EntrySet holds all candidates for an identical prefix and the currently
// winning one (first after sorting by AD, metric, sequence).
type EntrySet struct {
	Entries []Entry
}

func (s *EntrySet) best() *Entry {
	if s == nil || len(s.Entries) == 0 {
		return nil
	}
	best := s.Entries[0]
	return &best
}

// Snapshot is an immutable view of the table at one version. Snapshots are
// persistent: Apply builds a new snapshot sharing unchanged trie nodes.
type Snapshot struct {
	Version  uint64
	Seq      uint64
	V4       *trie.Trie[EntrySet]
	V6       *trie.Trie[EntrySet]
	byID     map[string]netmodel.Route
	MaxDepth int
}

// Persister stores one batch transactionally and returns the new version.
// Apply only swaps in the new snapshot after Persist returns nil.
type Persister interface {
	Persist(baseVersion, newVersion uint64, changes []Change, full []netmodel.Route) error
}

// ChangeKind distinguishes batch operations.
type ChangeKind string

const (
	Upsert ChangeKind = "upsert"
	Delete ChangeKind = "delete"
)

// Change is one operation inside a batch. The target route id is always
// Route.ID (the id field is deprecated and rejected by JSON parsing).
type Change struct {
	Kind  ChangeKind     `json:"kind"`
	Route netmodel.Route `json:"route"`
}

// UnmarshalJSON enforces change shape: delete needs only {"kind","route":{"id":...}};
// unknown kinds and the legacy top-level "id" are rejected.
func (c *Change) UnmarshalJSON(b []byte) error {
	type raw struct {
		Kind  ChangeKind       `json:"kind"`
		Route json.RawMessage  `json:"route"`
		ID    *json.RawMessage `json:"id"`
	}
	var r raw
	if err := json.Unmarshal(b, &r); err != nil {
		return err
	}
	if r.ID != nil {
		return fmt.Errorf("rib: change must identify the target via route.id, not top-level id")
	}
	if r.Kind != Upsert && r.Kind != Delete {
		return fmt.Errorf("rib: change kind %q invalid (want upsert|delete)", r.Kind)
	}
	if len(r.Route) == 0 {
		return fmt.Errorf("rib: %s change needs a route object", r.Kind)
	}
	c.Kind = r.Kind
	if r.Kind == Delete {
		var ref struct {
			ID      string `json:"id"`
			Prefix  string `json:"prefix"`
			NextHop *struct {
				Addr      string `json:"addr"`
				Interface string `json:"interface"`
			} `json:"next_hop"`
			AdminDist int    `json:"admin_distance"`
			Metric    uint32 `json:"metric"`
		}
		if err := json.Unmarshal(r.Route, &ref); err != nil {
			return err
		}
		if ref.ID == "" {
			return fmt.Errorf("rib: delete change needs route.id")
		}
		// Zero-valued placeholders produced by JSON round-trips ("", 0,
		// empty next_hop) are tolerated; populated route-defining fields are
		// not, because a delete addresses a route solely by id.
		if ref.Prefix != "" || ref.AdminDist != 0 || ref.Metric != 0 {
			return fmt.Errorf("rib: delete change for %s must only carry route.id", ref.ID)
		}
		if ref.NextHop != nil && (ref.NextHop.Addr != "" || ref.NextHop.Interface != "") {
			return fmt.Errorf("rib: delete change for %s must only carry route.id", ref.ID)
		}
		c.Route = netmodel.Route{ID: ref.ID}
		return nil
	}
	return json.Unmarshal(r.Route, &c.Route)
}

// ChangeError rejects one batch, naming the offending change index.
type ChangeError struct {
	Index  int
	Reason string
}

func (e *ChangeError) Error() string {
	return fmt.Sprintf("rib: change[%d] rejected: %s", e.Index, e.Reason)
}

// Table is the concurrency-safe RIB. Readers hold an atomic snapshot pointer;
// writers serialize on mu.
type Table struct {
	mu        sync.Mutex
	snap      atomic.Pointer[Snapshot]
	persister Persister
}

// NewTable creates a table with the given recursion depth limit and optional
// persister. The initial snapshot is version 0 (empty).
func NewTable(maxDepth int, p Persister) *Table {
	if maxDepth <= 0 {
		maxDepth = DefaultMaxDepth
	}
	t := &Table{persister: p}
	t.snap.Store(&Snapshot{
		Version:  0,
		V4:       trie.New[EntrySet](trie.IPv4Bits),
		V6:       trie.New[EntrySet](trie.IPv6Bits),
		byID:     map[string]netmodel.Route{},
		MaxDepth: maxDepth,
	})
	return t
}

// DefaultMaxDepth bounds recursive next-hop chains (hops, not table depth).
const DefaultMaxDepth = 8

// Current returns the active immutable snapshot.
func (t *Table) Current() *Snapshot { return t.snap.Load() }
