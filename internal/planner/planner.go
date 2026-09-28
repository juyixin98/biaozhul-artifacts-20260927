// Package planner compares a validated declared specification against an
// observation of the real world and emits an ordered, dependency-aware plan
// of creates / updates / replaces / deletes.
package planner

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sort"

	"infraplanner/internal/model"
	"infraplanner/internal/spec"
)

// OpType enumerates plan operations.
type OpType string

const (
	OpCreate  OpType = "create"
	OpUpdate  OpType = "update"
	OpReplace OpType = "replace"
	OpDelete  OpType = "delete"
	OpNoop    OpType = "noop"
)

// Operation is one planned change.
type Operation struct {
	Seq  int       `json:"seq"`
	Type OpType    `json:"type"`
	Key  model.Key `json:"key"`
	// ExistingID is the physical ID of the live resource for update/
	// replace/delete; empty for create.
	ExistingID string `json:"existing_id,omitempty"`
	// Detail explains why the op is required (diff reason).
	Detail string `json:"detail,omitempty"`
	// Changes lists mutable attribute/reference changes for update ops.
	Changes []Change `json:"changes,omitempty"`
}

// Change describes one field-level diff.
type Change struct {
	Field     string `json:"field"`
	Kind      string `json:"attr,omitempty"` // "attr" or "ref"
	Old       string `json:"old,omitempty"`
	New       string `json:"new,omitempty"`
	Immutable bool   `json:"immutable,omitempty"`
}

// Plan is the output of planning.
type Plan struct {
	Operations []Operation `json:"operations"`
	// Baseline is the exact observation the plan was built from. It is bound
	// to the plan and persisted so apply can refuse to run against drift.
	Baseline model.Observation `json:"baseline"`
	// Fingerprint binds the plan to its observation. Any change to the
	// observed world before application changes the fingerprint.
	Fingerprint string `json:"fingerprint"`
	// Guarded lists logical keys whose destroy/replace is blocked pending an
	// explicit guard release. Empty means the plan is immediately applicable.
	Guarded []model.Key `json:"guarded,omitempty"`
}

// Input bundles the planner inputs.
type Input struct {
	Spec     *spec.Spec
	Observed model.Observation
	// ReleasedGuards lists logical keys for which the caller has explicitly
	// lifted the destruction protection for this plan.
	ReleasedGuards map[model.Key]bool
}

// Build computes the plan. It never performs I/O.
func Build(in Input) (*Plan, error) {
	desired := in.Spec.ByKey()
	live := in.Observed.ByKey()
	released := in.ReleasedGuards
	if released == nil {
		released = map[model.Key]bool{}
	}

	type diff struct {
		key    model.Key
		op     OpType
		id     string
		chg    []Change
		detail string
		// guarded: op would destroy a protected resource without release.
		guarded bool
		// destructive: op removes a physical resource (delete/replace).
		destructive bool
	}

	diffs := map[model.Key]*diff{}
	var guarded []model.Key

	// creates / updates / replaces
	for k, d := range desired {
		l, exists := live[k]
		if !exists {
			diffs[k] = &diff{key: k, op: OpCreate, detail: "absent from observed world"}
			continue
		}
		ch := diffResource(d, l)
		if len(ch) == 0 {
			diffs[k] = &diff{key: k, op: OpNoop, id: l.ID, detail: "in sync"}
			continue
		}
		immutable := false
		for _, c := range ch {
			if c.Immutable {
				immutable = true
			}
		}
		// Protection is taken from the real resource and from the new spec:
		// either side marking it protected keeps the guard.
		protected := l.Protected || d.Protected
		if immutable {
			dd := &diff{key: k, op: OpReplace, id: l.ID, chg: ch,
				detail: reason(ch), destructive: true}
			if protected && !released[k] {
				dd.guarded = true
				guarded = append(guarded, k)
			}
			diffs[k] = dd
		} else {
			diffs[k] = &diff{key: k, op: OpUpdate, id: l.ID, chg: ch,
				detail: reason(ch)}
		}
	}

	// deletes
	for k, l := range live {
		if _, wanted := desired[k]; wanted {
			continue
		}
		dd := &diff{key: k, op: OpDelete, id: l.ID,
			detail: "no longer declared", destructive: true}
		if l.Protected && !released[k] {
			dd.guarded = true
			guarded = append(guarded, k)
		}
		diffs[k] = dd
	}

	// Cascade replacement: when an existing resource is replaced, every
	// existing transitive dependent is replaced as well, because its physical
	// reference (provider id) pointed at the resource being destroyed.
	// Dependents that do not exist yet (pure creates) are unaffected: they
	// resolve to the new ids during the creation wave.
	children := map[model.Key]map[model.Key]bool{} // parent -> children
	addChild := func(child, parent model.Key) {
		if children[parent] == nil {
			children[parent] = map[model.Key]bool{}
		}
		children[parent][child] = true
	}
	for k := range desired {
		d := desired[k]
		for _, dep := range in.Spec.Dependencies(d) {
			addChild(k, dep)
		}
	}
	for k, l := range live {
		ts := spec.Schema[l.Key.Kind]
		for rn := range ts.Refs {
			if ref, ok := l.Refs[rn]; ok {
				addChild(k, ref.Key())
			}
		}
	}

	var queue []model.Key
	for k, dd := range diffs {
		if dd.op == OpReplace {
			queue = append(queue, k)
		}
	}
	for len(queue) > 0 {
		parent := queue[0]
		queue = queue[1:]
		for child := range children[parent] {
			if _, existsLive := live[child]; !existsLive {
				continue // not yet created; will pick up the new id naturally
			}
			cd := diffs[child]
			if cd != nil && (cd.op == OpReplace || cd.op == OpDelete) {
				continue // already destructive; delete stays delete
			}
			d := desired[child]
			protected := live[child].Protected || d.Protected
			fd := &diff{
				key: child, op: OpReplace, id: live[child].ID,
				detail:      "forced replace: dependency " + parent.String() + " is replaced",
				chg:         nil,
				destructive: true,
			}
			if protected && !released[child] {
				fd.guarded = true
				guarded = append(guarded, child)
			}
			diffs[child] = fd
			queue = append(queue, child)
		}
	}

	// Build the dependency graph over every touched resource, merging desired
	// refs (for what will exist) and observed refs (for what will be removed).
	edges := map[model.Key]map[model.Key]bool{} // child -> set of parents
	nodes := map[model.Key]bool{}
	for k, dd := range diffs {
		if dd.op == OpNoop {
			continue
		}
		nodes[k] = true
		if d, ok := desired[k]; ok {
			for _, dep := range in.Spec.Dependencies(d) {
				// Edge to a dependency only matters for ordering if that
				// dependency is itself touched (delete/replace).
				if depDiff, touched := diffs[dep]; touched && depDiff.op != OpNoop {
					addEdge(edges, k, dep)
				}
			}
		}
		if l, ok := live[k]; ok {
			ts := spec.Schema[l.Key.Kind]
			for rn := range ts.Refs {
				if ref, ok := l.Refs[rn]; ok {
					dep := ref.Key()
					if depDiff, touched := diffs[dep]; touched && depDiff.op != OpNoop {
						addEdge(edges, k, dep)
					}
				}
			}
		}
	}

	ordered := topoSort(nodes, edges)

	// Emit sequence:
	//   1. deletes/replace-old removals in reverse create order
	//   2. creates / updates in forward create order
	// Replaces are split: the old resource is removed in the destruction
	// wave, the new one created in the creation wave.
	ops := make([]Operation, 0, len(ordered))
	seq := 0

	// destruction wave: reverse dependency order
	for i := len(ordered) - 1; i >= 0; i-- {
		k := ordered[i]
		dd := diffs[k]
		switch dd.op {
		case OpDelete:
			seq++
			ops = append(ops, Operation{Seq: seq, Type: OpDelete, Key: k,
				ExistingID: dd.id, Detail: dd.detail})
		case OpReplace:
			seq++
			ops = append(ops, Operation{Seq: seq, Type: OpDelete, Key: k,
				ExistingID: dd.id, Detail: "replace: destroy old (" + dd.detail + ")"})
		}
	}

	// creation/update wave: forward dependency order
	for _, k := range ordered {
		dd := diffs[k]
		switch dd.op {
		case OpCreate:
			seq++
			ops = append(ops, Operation{Seq: seq, Type: OpCreate, Key: k,
				Detail: dd.detail})
		case OpReplace:
			seq++
			ops = append(ops, Operation{Seq: seq, Type: OpCreate, Key: k,
				Detail: "replace: create new (" + dd.detail + ")", Changes: dd.chg})
		case OpUpdate:
			seq++
			ops = append(ops, Operation{Seq: seq, Type: OpUpdate, Key: k,
				ExistingID: dd.id, Detail: dd.detail, Changes: dd.chg})
		}
	}

	sort.Slice(guarded, func(i, j int) bool {
		if guarded[i].Kind != guarded[j].Kind {
			return model.Rank(guarded[i].Kind) < model.Rank(guarded[j].Kind)
		}
		return guarded[i].Name < guarded[j].Name
	})

	fp := Fingerprint(in.Observed)
	return &Plan{
		Operations:  ops,
		Baseline:    in.Observed,
		Fingerprint: fp,
		Guarded:     guarded,
	}, nil
}

func addEdge(edges map[model.Key]map[model.Key]bool, child, parent model.Key) {
	if edges[child] == nil {
		edges[child] = map[model.Key]bool{}
	}
	edges[child][parent] = true
}

// diffResource compares one desired resource against its live observation.
// A non-empty result means a change is required.
func diffResource(d model.Desired, l model.Live) []Change {
	var out []Change
	ts := spec.Schema[d.Kind]

	// attrs in schema order
	for _, f := range ts.Fields {
		dv := d.Attrs[f.Name]
		lv := l.Attrs[f.Name]
		// absence on both sides is equivalent
		if dv != lv {
			out = append(out, Change{Field: f.Name, Kind: "attr",
				Old: lv, New: dv, Immutable: f.Immutable})
		}
	}

	// refs in sorted name order
	refNames := make([]string, 0, len(ts.Refs))
	for rn := range ts.Refs {
		refNames = append(refNames, rn)
	}
	sort.Strings(refNames)
	for _, rn := range refNames {
		dv, dok := d.Refs[rn]
		lv, lok := l.Refs[rn]
		if !dok && !lok {
			continue
		}
		if !dok || !lok || dv != lv {
			oldS, newS := "", ""
			if lok {
				oldS = lv.Key().String()
			}
			if dok {
				newS = dv.Key().String()
			}
			// references are immutable: a changed reference is a replace.
			out = append(out, Change{Field: rn, Kind: "ref",
				Old: oldS, New: newS, Immutable: true})
		}
	}
	return out
}

func reason(ch []Change) string {
	out := ""
	for i, c := range ch {
		if i > 0 {
			out += ", "
		}
		if c.Kind == "ref" {
			out += c.Field + " " + c.Old + "->" + c.New
		} else {
			out += c.Field + "=" + c.New + " (was " + c.Old + ")"
		}
	}
	return out
}

// topoSort returns nodes so that every dependency precedes its dependents
// (create order). Ties are broken by kind rank then name for determinism.
func topoSort(nodes map[model.Key]bool, edges map[model.Key]map[model.Key]bool) []model.Key {
	indeg := map[model.Key]int{}
	for k := range nodes {
		indeg[k] = 0
	}
	for child, ps := range edges {
		for p := range ps {
			_ = indeg[p] // ensure present
			indeg[child]++
		}
	}

	less := func(a, b model.Key) bool {
		if model.Rank(a.Kind) != model.Rank(b.Kind) {
			return model.Rank(a.Kind) < model.Rank(b.Kind)
		}
		return a.Name < b.Name
	}

	ready := sortedKeys(nodes, indeg, less)
	var out []model.Key
	for len(ready) > 0 {
		// deterministic min-heap behaviour: keep ready sorted
		k := ready[0]
		ready = ready[1:]
		out = append(out, k)
		// find children of k
		for child, ps := range edges {
			if ps[k] {
				indeg[child]--
				if indeg[child] == 0 {
					ready = insertSorted(ready, child, less)
				}
			}
		}
	}
	return out
}

func sortedKeys(nodes map[model.Key]bool, indeg map[model.Key]int, less func(a, b model.Key) bool) []model.Key {
	var r []model.Key
	for k := range nodes {
		if indeg[k] == 0 {
			r = insertSorted(r, k, less)
		}
	}
	return r
}

func insertSorted(s []model.Key, k model.Key, less func(a, b model.Key) bool) []model.Key {
	i := sort.Search(len(s), func(i int) bool { return less(k, s[i]) })
	s = append(s, model.Key{})
	copy(s[i+1:], s[i:])
	s[i] = k
	return s
}

// Fingerprint is a stable digest of an observation's resource content.
// Live resources are sorted and serialized canonically so equal worlds hash
// equally. The observation timestamp is deliberately excluded: two reads of
// the same world taken at different instants must have the same fingerprint.
func Fingerprint(o model.Observation) string {
	lives := make([]model.Live, len(o.Resources))
	copy(lives, o.Resources)
	sort.Slice(lives, func(i, j int) bool {
		if lives[i].Key.Kind != lives[j].Key.Kind {
			return model.Rank(lives[i].Key.Kind) < model.Rank(lives[j].Key.Kind)
		}
		return lives[i].Key.Name < lives[j].Key.Name
	})
	// Canonicalize map fields so nil maps and empty maps hash identically;
	// both mean "no entries". Round-trip through JSON gives stable key order.
	canon := make([]model.Live, len(lives))
	for i, l := range lives {
		attrs := l.Attrs
		if attrs == nil {
			attrs = map[string]string{}
		}
		refs := l.Refs
		if refs == nil {
			refs = map[string]model.Ref{}
		}
		canon[i] = model.Live{Key: l.Key, ID: l.ID, Attrs: attrs, Refs: refs, Protected: l.Protected}
	}
	b, _ := json.Marshal(canon)
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}
