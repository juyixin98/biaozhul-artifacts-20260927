package merge_test

// This file contains an INDEPENDENT reference implementation of the merge
// rules. It deliberately uses different data structures (a flat leaf table)
// and a different algorithm (set operations on leaf paths, no tree walking)
// from the production engine in merge.go, so a shared bug cannot make both
// agree. Differential tests generate random apply sequences and cross-check
// that production and reference agree on live values, ownership and the
// conflict decision.
//
// Supported reference shape (covers every merge rule):
//
//	object leaves; keyed lists "containers" keyed by "name"; set list "tags".

import (
	"encoding/json"
	"fmt"
	"math/rand"
	"reflect"
	"sort"
	"testing"

	"fieldapply/internal/merge"
	"fieldapply/internal/model"
)

// refLeaf is one owned leaf in the reference world.
type refLeaf struct {
	path  string
	value any // decoded value (json.Number); nil marks an explicit null value
}

type refState struct {
	live   map[string]any // path -> value
	owners map[string]map[string]struct{}
	last   map[string]map[string]struct{} // manager -> set of paths in last apply
}

func newRefState() *refState {
	return &refState{
		live:   map[string]any{},
		owners: map[string]map[string]struct{}{},
		last:   map[string]map[string]struct{}{},
	}
}

func (r *refState) add(owner string, l refLeaf) { r.addPath(owner, l.path, l.value) }

func (r *refState) addPath(owner, path string, value any) {
	r.live[path] = value
	ms, ok := r.owners[path]
	if !ok {
		ms = map[string]struct{}{}
		r.owners[path] = ms
	}
	ms[owner] = struct{}{}
}

// refExtract flattens the small fixed-shape documents the differential tests
// use. Paths mirror the production renderer so the two tables line up.
func refExtract(doc any) []refLeaf {
	var out []refLeaf
	root, ok := doc.(map[string]any)
	if !ok {
		return out
	}
	for _, k := range []string{"replicas", "image", "note", "flag"} {
		if v, ok := root[k]; ok {
			out = append(out, refLeaf{path: "." + k, value: v})
		}
	}
	if specRaw, ok := root["spec"]; ok {
		spec := specRaw.(map[string]any)
		if v, ok := spec["replicas"]; ok {
			out = append(out, refLeaf{path: ".spec.replicas", value: v})
		}
		if tagsRaw, ok := spec["tags"]; ok {
			for _, t := range tagsRaw.([]any) {
				out = append(out, refLeaf{
					path:  fmt.Sprintf(".spec.tags[_=%s]", model.MustRaw(t)),
					value: t,
				})
			}
		}
		if csRaw, ok := spec["containers"]; ok {
			for _, c := range csRaw.([]any) {
				co := c.(map[string]any)
				name := co["name"].(string)
				base := fmt.Sprintf(`.spec.containers[name=%q]`, name)
				// The key field is itself an owned scalar leaf.
				out = append(out, refLeaf{path: base + ".name", value: name})
				for _, f := range []string{"image", "port"} {
					if v, ok := co[f]; ok {
						out = append(out, refLeaf{path: base + "." + f, value: v})
					}
				}
			}
		}
	}
	return out
}

// refApply is the naive three-way merge: set difference between previous and
// desired paths releases shares; desired leaves are claimed under the same
// owner/conflict rules as the spec. The apply is all-or-nothing: if any leaf
// conflicts, the state is left exactly as it was.
func (r *refState) refApply(manager string, config any, force bool) ([]model.Conflict, bool) {
	desired := map[string]any{}
	for _, l := range refExtract(config) {
		desired[l.path] = l.value
	}
	prev := r.last[manager]

	// Phase 1: detect conflicts against the current state. No mutation.
	var conflicts []model.Conflict
	paths := make([]string, 0, len(desired))
	for p := range desired {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	for _, p := range paths {
		v := desired[p]
		ms := r.owners[p]
		var others []string
		for m := range ms {
			if m != manager {
				others = append(others, m)
			}
		}
		sort.Strings(others)
		cur := r.live[p]
		if !jsonEqualRef(cur, v) && len(others) > 0 && !force {
			conflicts = append(conflicts, model.Conflict{
				Path: p, Owners: others,
				Current: model.MustRaw(cur), Applied: model.MustRaw(v),
			})
		}
	}
	if len(conflicts) > 0 {
		return conflicts, false
	}

	// Phase 2a: removals — previous paths absent from desired release shares.
	changed := false
	for p := range prev {
		if _, keep := desired[p]; keep {
			continue
		}
		if ms := r.owners[p]; ms != nil {
			delete(ms, manager)
			if len(ms) == 0 {
				delete(r.owners, p)
				delete(r.live, p)
				changed = true
			}
		}
	}

	// Phase 2b: claims. Force strips foreign shares only where a difference
	// would have conflicted; equal values simply add another share.
	for _, p := range paths {
		v := desired[p]
		ms := r.owners[p]
		if ms != nil && force && !jsonEqualRef(r.live[p], v) {
			for m := range ms {
				if m != manager {
					delete(ms, m)
				}
			}
		}
		if !jsonEqualRef(r.live[p], v) {
			changed = true
		}
		r.addPath(manager, p, v)
	}
	r.last[manager] = map[string]struct{}{}
	for p := range desired {
		r.last[manager][p] = struct{}{}
	}
	return nil, changed
}

func jsonEqualRef(a, b any) bool {
	raw := func(v any) string { return string(model.MustRaw(v)) }
	return raw(a) == raw(b)
}

// ---------------- differential harness ----------------

type refCase struct {
	managers [3]string
	docs     []any
	schema   model.Schema
}

func differentialSchema() model.Schema {
	return model.Schema{Lists: map[string]model.ListKind{
		".spec.tags":       model.ListSet,
		".spec.containers": model.ListKeyed,
	}, Keys: map[string]string{".spec.containers": "name"}}
}

func docsPool() []any {
	raws := []string{
		`{"replicas":1}`,
		`{"replicas":2}`,
		`{"image":"web:1"}`,
		`{"image":"web:2"}`,
		`{"flag":true}`,
		`{"spec":{"replicas":3}}`,
		`{"spec":{"replicas":4}}`,
		`{"spec":{"tags":["a"]}}`,
		`{"spec":{"tags":["a","b"]}}`,
		`{"spec":{"tags":["b","c"]}}`,
		`{"spec":{"containers":[{"name":"web","image":"w1","port":80}]}}`,
		`{"spec":{"containers":[{"name":"web","image":"w2","port":80}]}}`,
		`{"spec":{"containers":[{"name":"web","port":81}]}}`,
		`{"spec":{"containers":[{"name":"worker","image":"j1"}]}}`,
		`{"spec":{"containers":[{"name":"web","image":"w2","port":81},{"name":"worker","image":"j1"}]}}`,
		`{}`,
	}
	out := make([]any, len(raws))
	for i, r := range raws {
		out[i] = mustDecodeString(r)
	}
	return out
}

func mustDecodeString(s string) any {
	v, err := model.DecodeValue([]byte(s))
	if err != nil {
		panic(err)
	}
	return v
}

// TestDifferentialVsReference drives both implementations through hundreds of
// random interleaved applies and requires identical live leaf tables,
// ownership tables and conflict verdicts.
func TestDifferentialVsReference(t *testing.T) {
	rng := rand.New(rand.NewSource(20260928))
	schema := differentialSchema()
	docs := docsPool()
	managers := [3]string{"alice", "bob", "carol"}

	for run := 0; run < 400; run++ {
		ref := newRefState()

		// production state
		var live any = map[string]any{}
		owners := model.Owners{}
		prev := map[string]any{} // manager -> last config
		rev := int64(1)

		for step := 0; step < 12; step++ {
			mgr := managers[rng.Intn(3)]
			doc := docs[rng.Intn(len(docs))]
			force := rng.Intn(5) == 0 // 20% forced

			// reference
			rConflicts, _ := ref.refApply(mgr, doc, force)

			// production
			res, err := merge.Apply(&merge.Inputs{
				ResourceID: "r", Manager: mgr,
				Live: live, Owners: owners,
				Previous: prev[mgr],
				Config:   cloneDoc(doc),
				Force:    force,
				Schema:   schema, Rev: rev,
			})

			if len(rConflicts) > 0 {
				if err == nil {
					t.Fatalf("run %d step %d %s force=%v: reference saw conflicts %v, production applied",
						run, step, mgr, force, pathsOf(rConflicts))
				}
				se, _ := model.AsError(err)
				if se.Category != model.CatStateConflict || se.Code != "field_conflict" {
					t.Fatalf("run %d step %d: wrong error %s/%s", run, step, se.Category, se.Code)
				}
				if !sameConflictSet(se.Conflicts, rConflicts) {
					t.Fatalf("run %d step %d %s doc=%s force=%v\n prod=%v\n ref =%v",
						run, step, mgr, model.MustRaw(doc), force,
						se.Conflicts, rConflicts)
				}
				// no state change on either side
				continue
			}
			if err != nil {
				t.Fatalf("run %d step %d %s doc=%s force=%v: production errored but reference clean: %v",
					run, step, mgr, model.MustRaw(doc), force, err)
			}
			live = res.Live
			owners = res.Owners
			prev[mgr] = cloneDoc(doc)
			rev++

			// compare live leaf tables
			prodLeaves := merge.Leaves(live, &schema)
			if !leafTablesEqual(prodLeaves, ref.live) {
				t.Fatalf("run %d step %d %s doc=%s force=%v\n prod live=%s\n ref live =%s",
					run, step, mgr, model.MustRaw(doc), force,
					model.MustRaw(prodLeaves), model.MustRaw(ref.live))
			}
			// compare ownership
			if !ownersEqual(owners, ref.owners) {
				t.Fatalf("run %d step %d %s doc=%s force=%v\n prod own=%s\n ref own =%s",
					run, step, mgr, model.MustRaw(doc), force,
					model.MustRaw(ownersView(owners)), model.MustRaw(ref.owners))
			}
		}
	}
}

func cloneDoc(v any) any {
	b, _ := json.Marshal(v)
	out, _ := model.DecodeValue(b)
	return out
}

func pathsOf(cs []model.Conflict) []string {
	out := make([]string, len(cs))
	for i, c := range cs {
		out[i] = c.Path
	}
	sort.Strings(out)
	return out
}

func sameConflictSet(a, b []model.Conflict) bool {
	key := func(cs []model.Conflict) map[string][]string {
		m := map[string][]string{}
		for _, c := range cs {
			owners := append([]string(nil), c.Owners...)
			sort.Strings(owners)
			m[c.Path] = owners
		}
		return m
	}
	return reflect.DeepEqual(key(a), key(b))
}

func leafTablesEqual(a map[string]any, b map[string]any) bool {
	if len(a) != len(b) {
		return false
	}
	for k, av := range a {
		bv, ok := b[k]
		if !ok || !merge.EqualJSON(av, bv) {
			return false
		}
	}
	return true
}

func ownersEqual(a model.Owners, b map[string]map[string]struct{}) bool {
	if len(a) != len(b) {
		return false
	}
	for p, ms := range a {
		bs := b[p]
		if len(ms) != len(bs) {
			return false
		}
		for m := range ms {
			if _, ok := bs[m]; !ok {
				return false
			}
		}
	}
	return true
}

func ownersView(o model.Owners) map[string][]string {
	out := map[string][]string{}
	for p, ms := range o {
		var names []string
		for m := range ms {
			names = append(names, m)
		}
		sort.Strings(names)
		out[p] = names
	}
	return out
}
