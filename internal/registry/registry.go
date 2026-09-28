// Package registry holds the schema of each resource kind: which attributes
// are immutable (change forces replacement), which are required, and how raw
// attributes are canonicalized before comparison.
package registry

import (
	"sort"
	"strings"

	"infraplanner/internal/errorsx"
	"infraplanner/internal/model"
)

// KindDef is the schema contract for one resource kind.
type KindDef struct {
	Kind      model.Kind
	Required  []string
	Immutable []string // attributes whose change forces replace
	// Normalize maps a raw attribute value to its canonical form. nil means
	// the trimmed string is used verbatim.
	Normalize map[string]func(string) string
}

var defs = map[model.Kind]*KindDef{}

func Register(d *KindDef) { defs[d.Kind] = d }

func init() {
	ident := func(s string) string { return strings.TrimSpace(s) }
	lower := func(s string) string { return strings.ToLower(strings.TrimSpace(s)) }

	Register(&KindDef{
		Kind:      model.KindNetwork,
		Required:  []string{"cidr"},
		Immutable: []string{"cidr"},
		Normalize: map[string]func(string) string{"cidr": ident, "name": ident},
	})
	Register(&KindDef{
		Kind:      model.KindSubnet,
		Required:  []string{"cidr"},
		Immutable: []string{"cidr"}, // parent network is a DependsOn reference
		Normalize: map[string]func(string) string{"cidr": ident, "zone": lower, "name": ident},
	})
	Register(&KindDef{
		Kind:      model.KindInstance,
		Required:  []string{"image", "flavor"},
		Immutable: []string{"image"}, // flavor is mutable in place
		Normalize: map[string]func(string) string{"image": ident, "flavor": lower, "name": ident},
	})
	Register(&KindDef{
		Kind:      model.KindBucket,
		Required:  []string{"name"},
		Immutable: []string{"name"},
		Normalize: map[string]func(string) string{"name": lower},
	})
	Register(&KindDef{
		Kind:      model.KindDisk,
		Required:  []string{"size_gb"},
		Immutable: []string{"size_gb", "backing"},
		Normalize: map[string]func(string) string{"size_gb": ident, "backing": lower},
	})
}

// Get returns the definition of a registered kind.
func Get(k model.Kind) (*KindDef, bool) {
	d, ok := defs[k]
	return d, ok
}

// Canonical returns the canonical attribute map for a raw map: required attrs
// validated, values normalized, keys sorted for stable hashing.
func (d *KindDef) Canonical(raw map[string]string) (map[string]string, error) {
	out := make(map[string]string, len(raw))
	for _, req := range d.Required {
		v, ok := raw[req]
		if !ok || strings.TrimSpace(v) == "" {
			return nil, errorsx.Input("MISSING_ATTR",
				"required attribute missing", map[string]any{
					"kind": string(d.Kind), "attribute": req,
				})
		}
		out[req] = d.norm(req, v)
	}
	for k, v := range raw {
		out[k] = d.norm(k, v)
	}
	return out, nil
}

func (d *KindDef) norm(k, v string) string {
	if f, ok := d.Normalize[k]; ok {
		return f(v)
	}
	return strings.TrimSpace(v)
}

// ImmutableSet returns the immutable attributes as a lookup set.
func (d *KindDef) ImmutableSet() map[string]bool {
	m := make(map[string]bool, len(d.Immutable))
	for _, a := range d.Immutable {
		m[a] = true
	}
	return m
}

// CanonicalSignature renders attributes as a stable "k=v" string, joined by
// newlines in sorted key order. Used in the observation digest.
func CanonicalSignature(attrs map[string]string) string {
	keys := make([]string, 0, len(attrs))
	for k := range attrs {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+attrs[k])
	}
	return strings.Join(parts, "\n")
}
