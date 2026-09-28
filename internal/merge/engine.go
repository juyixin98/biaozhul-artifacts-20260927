package merge

import (
	"encoding/json"
	"reflect"
	"sort"

	"fieldmerge/internal/apperr"
	"fieldmerge/internal/fieldpath"
	"fieldmerge/internal/schema"
)

// Apply performs the field-level three-way merge.
//
// Inputs: the applying manager's full declaration (Config), the currently
// merged live object (Live), the semantic Schema and the current ownership
// Claims. Ownership makes this a three-way merge: each field knows which
// manager "owns" it; a manager may only write fields it owns or that are
// unowned, and taking a field from another manager is rejected unless Force
// is set.
//
// The engine is pure — no storage, clocks or I/O. On conflicts the returned
// Result still carries the would-be Live and the complete conflict list, but
// callers MUST NOT persist it.
func Apply(in Input, claims []Claim) (*Result, error) {
	if in.Manager == "" {
		return nil, apperr.New(apperr.InvalidInput, "manager_required", "manager must be non-empty")
	}
	if in.Kind == "" || in.Name == "" {
		return nil, apperr.New(apperr.InvalidInput, "resource_id_required", "kind and name are required")
	}
	e, err := newEngine(in, claims)
	if err != nil {
		return nil, err
	}
	merged, alive, err := e.walkObject(e.cfg0, asMap(e.live0),
		fieldpath.Path{}, []string{}, "")
	if err != nil {
		return nil, err
	}
	if !alive {
		merged = map[string]any{} // a resource is always a JSON object
	}

	res := &Result{
		Kind: in.Kind, Name: in.Name, Live: merged,
		Conflict: e.conflicts, Changes: e.changes,
	}

	// Reconcile claims with what actually exists in the merged tree: claims
	// pointing at vanished fields are stale and are pruned (recorded for the
	// audit log) rather than resurrected on the next apply.
	surviving := map[string]map[string]struct{}{}
	var pruned []string
	for p, mgrs := range e.claims {
		fp, err := fieldpath.Parse(p)
		if err != nil {
			return nil, apperr.New(apperr.Internal, "ownership_corrupt",
				"stored ownership path %q is unparseable: %v", p, err)
		}
		if _, ok := fieldpath.Lookup(merged, fp); ok {
			surviving[p] = mgrs
		} else {
			pruned = append(pruned, p)
		}
	}
	sort.Strings(pruned)
	res.PrunedOwnership = pruned
	res.Claims = encodeClaims(surviving)
	return res, nil
}

type engine struct {
	in     Input
	schema *schema.Schema
	mgr    string
	force  bool
	live0  any
	cfg0   map[string]any

	claims map[string]map[string]struct{} // path -> set(managers)

	conflicts  []Conflict
	changes    []Change
	changeSeen map[string]struct{}
}

func newEngine(in Input, rows []Claim) (*engine, error) {
	own := map[string]map[string]struct{}{}
	for _, c := range rows {
		fp, err := fieldpath.Parse(c.Path)
		if err != nil {
			return nil, apperr.New(apperr.Internal, "ownership_corrupt",
				"stored ownership path %q is unparseable: %v", c.Path, err)
		}
		set := own[fp.String()]
		if set == nil {
			set = map[string]struct{}{}
			own[fp.String()] = set
		}
		for _, m := range c.Managers {
			if m != "" {
				set[m] = struct{}{}
			}
		}
	}
	var live any
	var cfg map[string]any
	if len(in.Live) > 0 {
		if err := json.Unmarshal(in.Live, &live); err != nil {
			return nil, apperr.New(apperr.ComputeFailure, "live_bad_json",
				"stored live object is not valid JSON: %v", err)
		}
		if _, ok := live.(map[string]any); !ok {
			return nil, apperr.New(apperr.ComputeFailure, "live_not_object",
				"stored live object must be a JSON object; got %T", live)
		}
	}
	if len(in.Config) > 0 {
		var c any
		if err := json.Unmarshal(in.Config, &c); err != nil {
			return nil, apperr.New(apperr.InvalidInput, "config_bad_json",
				"config is not valid JSON: %v", err)
		}
		cm, ok := c.(map[string]any)
		if !ok {
			return nil, apperr.New(apperr.InvalidInput, "config_not_object",
				"apply config must be a JSON object; got %T", c)
		}
		cfg = cm
	}
	return &engine{
		in: in, schema: in.Schema, mgr: in.Manager, force: in.Force,
		live0: live, cfg0: cfg, claims: own,
		changeSeen: map[string]struct{}{},
	}, nil
}

// ---- claim helpers ----

func encodeClaims(own map[string]map[string]struct{}) []Claim {
	paths := make([]string, 0, len(own))
	for p, set := range own {
		if len(set) > 0 {
			paths = append(paths, p)
		}
	}
	sort.Strings(paths)
	out := make([]Claim, 0, len(paths))
	for _, p := range paths {
		ms := make([]string, 0, len(own[p]))
		for m := range own[p] {
			ms = append(ms, m)
		}
		sort.Strings(ms)
		out = append(out, Claim{Path: p, Managers: ms})
	}
	return out
}

// foreignUnder returns sorted foreign owners of p itself or any claim below p.
func (e *engine) foreignUnder(p fieldpath.Path) []string {
	seen := map[string]struct{}{}
	for cp, set := range e.claims {
		cfp, _ := fieldpath.Parse(cp)
		if cfp.HasPrefix(p) {
			for m := range set {
				if m != e.mgr {
					seen[m] = struct{}{}
				}
			}
		}
	}
	var out []string
	for m := range seen {
		out = append(out, m)
	}
	sort.Strings(out)
	return out
}

func (e *engine) setOwners(p fieldpath.Path, managers ...string) {
	key := p.String()
	set := map[string]struct{}{}
	for _, m := range managers {
		if m != "" {
			set[m] = struct{}{}
		}
	}
	if len(set) == 0 {
		delete(e.claims, key)
	} else {
		e.claims[key] = set
	}
}

// dropForeignUnder removes other managers from the claim at p and every claim
// below p (force semantics). Self-claims and shared sets are preserved.
func (e *engine) dropForeignUnder(p fieldpath.Path) {
	for cp := range e.claims {
		cfp, _ := fieldpath.Parse(cp)
		if cfp.HasPrefix(p) {
			set := e.claims[cp]
			for m := range set {
				if m != e.mgr {
					delete(set, m)
				}
			}
			if len(set) == 0 {
				delete(e.claims, cp)
			}
		}
	}
}

func (e *engine) addConflict(p fieldpath.Path, owners []string, reason, wanted string) {
	for _, c := range e.conflicts {
		if c.Path == p.String() && c.Reason == reason {
			return
		}
	}
	e.conflicts = append(e.conflicts, Conflict{
		Path: p.String(), Owners: owners, Reason: reason, Wanted: wanted,
	})
}

func (e *engine) addChange(p fieldpath.Path, op, reason string, from, to any) {
	key := p.String() + "|" + op
	if _, ok := e.changeSeen[key]; ok {
		return
	}
	e.changeSeen[key] = struct{}{}
	c := Change{Path: p.String(), Op: op, Reason: reason}
	if from != nil {
		b, _ := json.Marshal(from)
		c.From = b
	}
	if to != nil {
		b, _ := json.Marshal(to)
		c.To = b
	}
	e.changes = append(e.changes, c)
}

func deepEqual(a, b any) bool { return reflect.DeepEqual(a, b) }

func asMap(v any) map[string]any {
	if v == nil {
		return nil
	}
	m, _ := v.(map[string]any)
	return m
}

func asSlice(v any) []any {
	if v == nil {
		return nil
	}
	s, _ := v.([]any)
	return s
}
