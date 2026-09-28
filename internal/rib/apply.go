package rib

import (
	"fmt"
	"sort"

	"github.com/opp221/ribd/internal/netmodel"
	"github.com/opp221/ribd/internal/trie"
)

// build validates and applies a batch onto a new snapshot. It performs no I/O
// and never mutates the receiver; Apply decides whether to persist and swap.
func (s *Snapshot) build(changes []Change) (*Snapshot, error) {
	ns := &Snapshot{
		Version:  s.Version + 1,
		Seq:      s.Seq,
		V4:       s.V4,
		V6:       s.V6,
		byID:     make(map[string]netmodel.Route, len(s.byID)),
		MaxDepth: s.MaxDepth,
	}
	for id, r := range s.byID {
		ns.byID[id] = r
	}

	for i, ch := range changes {
		switch ch.Kind {
		case Upsert:
			r := ch.Route
			if err := r.Validate(); err != nil {
				return nil, &ChangeError{Index: i, Reason: err.Error()}
			}
			if existing, ok := ns.byID[r.ID]; ok && existing.Prefix != r.Prefix {
				var err error
				ns, err = removeEntry(ns, existing)
				if err != nil {
					return nil, &ChangeError{Index: i, Reason: err.Error()}
				}
			}
			var err error
			ns, err = upsertEntry(ns, r)
			if err != nil {
				return nil, &ChangeError{Index: i, Reason: err.Error()}
			}
		case Delete:
			existing, ok := ns.byID[ch.Route.ID]
			if !ok {
				return nil, &ChangeError{Index: i, Reason: "delete refers to unknown route id " + ch.Route.ID}
			}
			var err error
			ns, err = removeEntry(ns, existing)
			if err != nil {
				return nil, &ChangeError{Index: i, Reason: err.Error()}
			}
		}
	}
	return ns, nil
}

// treeFor returns the trie for a family.
func treeFor(s *Snapshot, f netmodel.Family) *trie.Trie[EntrySet] {
	if f == netmodel.FamilyV4 {
		return s.V4
	}
	return s.V6
}

// exactSet returns the candidate set at an exact prefix.
func exactSet(s *Snapshot, p netmodel.Prefix) (*EntrySet, bool, error) {
	tree := treeFor(s, p.Family())
	chain, err := tree.MatchChain(p.Addr().Bytes())
	if err != nil {
		return nil, false, err
	}
	for i := range chain {
		if chain[i].KeyLen == p.Bits() {
			return &chain[i].Value, true, nil
		}
	}
	return nil, false, nil
}

func upsertEntry(s *Snapshot, r netmodel.Route) (*Snapshot, error) {
	seq := r.Seq
	if existing, ok := s.byID[r.ID]; ok && existing.Prefix == r.Prefix {
		// Re-announce keeps installation seniority; it may change AD/metric.
		seq = existing.Seq
	} else {
		s.Seq++
		seq = s.Seq
	}
	r.Seq = seq
	r.InstalledVersion = s.Version

	set, _, err := exactSet(s, r.Prefix)
	if err != nil {
		return nil, err
	}
	var entries []Entry
	if set != nil {
		for _, e := range set.Entries {
			if e.Route.ID != r.ID {
				entries = append(entries, e)
			}
		}
	}
	entries = append(entries, Entry{Route: r})
	sortEntries(entries)

	tree := treeFor(s, r.Family())
	key := r.Prefix.Addr().Bytes()
	nt, err := tree.Insert(key, r.Prefix.Bits(), EntrySet{Entries: entries})
	if err != nil {
		return nil, err
	}
	if r.Family() == netmodel.FamilyV4 {
		s.V4 = nt
	} else {
		s.V6 = nt
	}
	s.byID[r.ID] = r
	return s, nil
}

func removeEntry(s *Snapshot, r netmodel.Route) (*Snapshot, error) {
	set, ok, err := exactSet(s, r.Prefix)
	if err != nil {
		return nil, err
	}
	if !ok {
		delete(s.byID, r.ID)
		return s, nil
	}
	var entries []Entry
	for _, e := range set.Entries {
		if e.Route.ID != r.ID {
			entries = append(entries, e)
		}
	}
	tree := treeFor(s, r.Family())
	key := r.Prefix.Addr().Bytes()
	if len(entries) == 0 {
		nt, _, err := tree.Delete(key, r.Prefix.Bits())
		if err != nil {
			return nil, err
		}
		if r.Family() == netmodel.FamilyV4 {
			s.V4 = nt
		} else {
			s.V6 = nt
		}
	} else {
		sortEntries(entries)
		nt, err := tree.Insert(key, r.Prefix.Bits(), EntrySet{Entries: entries})
		if err != nil {
			return nil, err
		}
		if r.Family() == netmodel.FamilyV4 {
			s.V4 = nt
		} else {
			s.V6 = nt
		}
	}
	delete(s.byID, r.ID)
	return s, nil
}

func sortEntries(e []Entry) {
	sort.SliceStable(e, func(i, j int) bool {
		a, b := e[i].Route, e[j].Route
		if a.AdminDist != b.AdminDist {
			return a.AdminDist < b.AdminDist
		}
		if a.Metric != b.Metric {
			return a.Metric < b.Metric
		}
		return a.Seq < b.Seq
	})
}

// Apply runs a batch atomically: validate -> persist -> swap. On any error the
// live snapshot is untouched.
func (t *Table) Apply(changes []Change) (*Snapshot, error) {
	t.mu.Lock()
	defer t.mu.Unlock()

	cur := t.snap.Load()
	ns, err := cur.build(changes)
	if err != nil {
		return cur, err
	}
	if t.persister != nil {
		if err := t.persister.Persist(cur.Version, ns.Version, changes, ns.Routes()); err != nil {
			return cur, err
		}
	}
	t.snap.Store(ns)
	return ns, nil
}

// Adopt replaces live state with a reconstructed snapshot (cold-start replay).
// It is only valid over the empty version-0 table; adoption never persists.
func (t *Table) Adopt(s *Snapshot) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	cur := t.snap.Load()
	if cur.Version != 0 {
		return fmt.Errorf("rib: adopt called at non-empty version %d", cur.Version)
	}
	if s.MaxDepth != cur.MaxDepth {
		return fmt.Errorf("rib: replay max_depth %d differs from configured %d", s.MaxDepth, cur.MaxDepth)
	}
	t.snap.Store(s)
	return nil
}

// Routes returns every installed route in deterministic order: IPv4 before
// IPv6, prefix length ascending, then network address, then best candidate
// first (which after sorting is AD/metric/seq order).
func (s *Snapshot) Routes() []netmodel.Route {
	var out []netmodel.Route
	collect := func(tr interface {
		Each(func(int, EntrySet) bool)
	}) {
		tr.Each(func(klen int, set EntrySet) bool {
			for _, e := range set.Entries {
				out = append(out, e.Route)
			}
			return true
		})
	}
	collect(s.V4)
	collect(s.V6)
	return out
}

// VersionedVersion returns the version of the snapshot.
func (s *Snapshot) VersionNum() uint64 { return s.Version }

// RouteByID returns the installed route with that id.
func (s *Snapshot) RouteByID(id string) (netmodel.Route, bool) {
	r, ok := s.byID[id]
	return r, ok
}

// BuildForAPI validates a batch against this snapshot without touching live
// state; the returned candidate snapshot is discarded by callers that only
// need validation (dry run).
func (s *Snapshot) BuildForAPI(changes []Change) (*Snapshot, error) {
	return s.build(changes)
}

// MaxDepthNum exposes the recursion limit for API requests.
func (s *Snapshot) MaxDepthNum() int { return s.MaxDepth }
