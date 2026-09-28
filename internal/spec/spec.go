// Package spec defines the supported resource schema and validates a declared
// specification. Validation is the single place that produces input_error
// results for malformed declarations.
package spec

import (
	"fmt"
	"sort"

	"infraplanner/internal/model"
)

// Field describes one schema attribute.
type Field struct {
	Name      string
	Required  bool
	Immutable bool // change requires replacement
}

// TypeSchema describes one resource kind.
type TypeSchema struct {
	Kind   model.Kind
	Fields []Field
	// Refs maps reference attribute name -> the kind it must point at.
	Refs map[string]model.Kind
}

// Schema is the registry of supported types.
var Schema = map[model.Kind]TypeSchema{
	model.KindVPC: {
		Kind: model.KindVPC,
		Fields: []Field{
			{Name: "cidr", Required: true, Immutable: true},
			{Name: "region", Required: true, Immutable: true},
			{Name: "tags", Required: false, Immutable: false},
		},
		Refs: map[string]model.Kind{},
	},
	model.KindSubnet: {
		Kind: model.KindSubnet,
		Fields: []Field{
			{Name: "cidr", Required: true, Immutable: true},
			{Name: "zone", Required: true, Immutable: false},
			{Name: "tags", Required: false, Immutable: false},
		},
		// network_ref is immutable: moving a subnet to another VPC is a
		// replace, not an in-place edit.
		Refs: map[string]model.Kind{"network_ref": model.KindVPC},
	},
	model.KindInstance: {
		Kind: model.KindInstance,
		Fields: []Field{
			{Name: "image", Required: true, Immutable: true},
			{Name: "shape", Required: true, Immutable: false},
			{Name: "state", Required: false, Immutable: false}, // running|stopped
		},
		Refs: map[string]model.Kind{"subnet_ref": model.KindSubnet},
	},
	model.KindBucket: {
		Kind: model.KindBucket,
		Fields: []Field{
			{Name: "storage_class", Required: false, Immutable: true},
			{Name: "versioning", Required: false, Immutable: false},
		},
		Refs: map[string]model.Kind{},
	},
}

// Spec is a validated desired specification.
type Spec struct {
	Resources []model.Desired
	byKey     map[model.Key]model.Desired
}

// Parse validates raw desired resources and returns a Spec. All validation
// failures are returned as model errors with category input_error; the
// returned message lists every problem found in one pass.
func Parse(resources []model.Desired) (*Spec, error) {
	var problems []string
	byKey := map[model.Key]model.Desired{}
	keys := make([]model.Key, 0, len(resources))

	addProblem := func(f string, a ...any) { problems = append(problems, fmt.Sprintf(f, a...)) }

	for i, d := range resources {
		k := d.Key()
		if !k.Valid() {
			addProblem("resource[%d]: unknown kind %q or empty name", i, d.Kind)
			continue
		}
		if _, dup := byKey[k]; dup {
			addProblem("resource %s: duplicate logical name", k)
			continue
		}
		ts, ok := Schema[d.Kind]
		if !ok {
			addProblem("resource %s: unknown kind", k)
			continue
		}
		byKey[k] = d
		keys = append(keys, k)

		// scalar attributes
		known := map[string]bool{}
		for _, f := range ts.Fields {
			known[f.Name] = true
			if f.Required {
				v, present := d.Attrs[f.Name]
				if !present || v == "" {
					addProblem("resource %s: missing required attribute %q", k, f.Name)
				}
			}
		}
		for an := range d.Attrs {
			if !known[an] {
				addProblem("resource %s: unknown attribute %q", k, an)
			}
		}

		// references
		knownRefs := map[string]bool{}
		for rn, targetKind := range ts.Refs {
			knownRefs[rn] = true
			ref, present := d.Refs[rn]
			if !present {
				addProblem("resource %s: missing required reference %q", k, rn)
				continue
			}
			if ref.Kind != targetKind || ref.Name == "" {
				addProblem("resource %s: reference %q must point at %s/<name>, got %s",
					k, rn, targetKind, ref.Key())
			}
		}
		for rn := range d.Refs {
			if !knownRefs[rn] {
				addProblem("resource %s: unknown reference %q", k, rn)
				continue
			}
		}
	}

	// referential integrity: every referenced name must exist in the spec.
	for _, k := range keys {
		d := byKey[k]
		ts := Schema[d.Kind]
		for rn, wantKind := range ts.Refs {
			ref := d.Refs[rn]
			target := model.Key{Kind: ref.Kind, Name: ref.Name}
			t, ok := byKey[target]
			if !ok {
				addProblem("resource %s: reference %q -> %s does not exist in the spec",
					k, rn, target)
				continue
			}
			if t.Kind != wantKind {
				addProblem("resource %s: reference %q -> %s has wrong kind", k, rn, target)
			}
			if target == k {
				addProblem("resource %s: reference %q points at itself", k, rn)
			}
		}
	}

	// reference cycle detection over the desired graph.
	if cyc := findCycle(byKey); cyc != "" {
		addProblem("reference cycle detected: %s", cyc)
	}

	if len(problems) > 0 {
		sort.Strings(problems)
		return nil, model.E(model.CatInput, "invalid_spec", "%d problem(s): %s",
			len(problems), joinProblems(problems))
	}
	return &Spec{Resources: resources, byKey: byKey}, nil
}

// Get returns the desired resource for a key.
func (s *Spec) Get(k model.Key) (model.Desired, bool) {
	d, ok := s.byKey[k]
	return d, ok
}

// ByKey exposes the index.
func (s *Spec) ByKey() map[model.Key]model.Desired { return s.byKey }

// Dependencies returns the keys this resource references (its create-order deps).
func (s *Spec) Dependencies(d model.Desired) []model.Key {
	ts := Schema[d.Kind]
	out := make([]model.Key, 0, len(ts.Refs))
	for _, rn := range refNames(ts) {
		out = append(out, d.Refs[rn].Key())
	}
	return out
}

func refNames(ts TypeSchema) []string {
	names := make([]string, 0, len(ts.Refs))
	for rn := range ts.Refs {
		names = append(names, rn)
	}
	sort.Strings(names)
	return names
}

func joinProblems(p []string) string {
	out := ""
	for i, s := range p {
		if i > 0 {
			out += "; "
		}
		out += s
	}
	return out
}

// findCycle returns a textual cycle path or "".
func findCycle(byKey map[model.Key]model.Desired) string {
	const (
		white = 0
		gray  = 1
		black = 2
	)
	color := map[model.Key]int{}
	var stack []model.Key

	var dfs func(k model.Key) []model.Key
	dfs = func(k model.Key) []model.Key {
		color[k] = gray
		stack = append(stack, k)
		d := byKey[k]
		ts := Schema[d.Kind]
		for _, rn := range refNames(ts) {
			next := d.Refs[rn].Key()
			t, ok := byKey[next]
			if !ok {
				continue
			}
			_ = t
			switch color[next] {
			case white:
				if cyc := dfs(next); cyc != nil {
					return cyc
				}
			case gray:
				// found back edge: extract cycle from stack.
				for i, sk := range stack {
					if sk == next {
						return append(append([]model.Key{}, stack[i:]...), next)
					}
				}
			}
		}
		stack = stack[:len(stack)-1]
		color[k] = black
		return nil
	}

	keys := make([]model.Key, 0, len(byKey))
	for k := range byKey {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].Kind != keys[j].Kind {
			return model.Rank(keys[i].Kind) < model.Rank(keys[j].Kind)
		}
		return keys[i].Name < keys[j].Name
	})
	for _, k := range keys {
		if color[k] == white {
			if cyc := dfs(k); cyc != nil {
				out := ""
				for i, ck := range cyc {
					if i > 0 {
						out += " -> "
					}
					out += ck.String()
				}
				return out
			}
		}
	}
	return ""
}
