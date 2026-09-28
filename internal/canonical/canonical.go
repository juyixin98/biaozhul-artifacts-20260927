// Package canonical turns raw desired specs and observed reality into stable,
// comparable canonical forms, and computes the observation digest that a plan
// is bound to. Any change between observe and apply flips the digest and causes
// the apply to be refused as a state conflict (drift).
package canonical

import (
	"crypto/sha256"
	"encoding/hex"
	"sort"
	"strings"

	"infraplanner/internal/errorsx"
	"infraplanner/internal/model"
	"infraplanner/internal/registry"
)

// CanonicalDesired validates and canonicalizes a desired set. It returns the
// canonical specs (same slices/maps, attributes normalized) or an input error.
func CanonicalDesired(d *model.DesiredSet) ([]model.Spec, error) {
	byID, err := d.ByID()
	if err != nil {
		return nil, errorsx.Input("INVALID_DESIRED", err.Error(), nil)
	}
	out := make([]model.Spec, 0, len(d.Resources))
	for _, s := range d.Resources {
		def, ok := registry.Get(s.Kind)
		if !ok {
			return nil, errorsx.Input("UNKNOWN_KIND", "resource kind is not registered",
				map[string]any{"kind": string(s.Kind), "id": s.ID})
		}
		attrs, err := def.Canonical(s.Attrs)
		if err != nil {
			e, _ := errorsx.AsError(err)
			e.Evidence["id"] = s.ID
			return nil, e
		}
		deps := append([]string(nil), s.DependsOn...)
		sort.Strings(deps)
		// DependsOn target must be a declared resource (checked in dag.Build);
		// here we only reject blanks.
		for _, dep := range deps {
			if strings.TrimSpace(dep) == "" {
				return nil, errorsx.Input("BLANK_REF", "empty dependency reference",
					map[string]any{"id": s.ID})
			}
			if _, ok := byID[dep]; !ok {
				return nil, errorsx.Input("UNDECLARED_REF",
					"dependency is not declared in this desired set",
					map[string]any{"id": s.ID, "ref": dep})
			}
		}
		out = append(out, model.Spec{
			Kind:      s.Kind,
			ID:        s.ID,
			DependsOn: deps,
			Attrs:     attrs,
			Protected: s.Protected,
		})
	}
	// deterministic order: by id
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out, nil
}

// ObservationDigest is a content hash of observed reality: every resource's
// ref, state, attributes, protection and token. The plan binds to this digest.
func ObservationDigest(obs *model.ObservedSet) string {
	keys := make([]string, 0, len(obs.Resources))
	for k := range obs.Resources {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	h := sha256.New()
	write := func(s string) { h.Write([]byte(s)); h.Write([]byte{0}) }
	for _, k := range keys {
		r := obs.Resources[k]
		write(r.Ref.String())
		write(string(r.State))
		write(boolStr(r.Protected))
		write(boolStr(r.External))
		write(r.ProviderToken)
		deps := append([]string(nil), r.DependsOn...)
		sort.Strings(deps)
		write(strings.Join(deps, ","))
		write(registry.CanonicalSignature(r.Attrs))
		h.Write([]byte{'\n'})
	}
	return "sha256:" + hex.EncodeToString(h.Sum(nil))
}

// DesiredDigest hashes canonical desired specs, useful for evidence/logs and
// idempotent plan caching.
func DesiredDigest(specs []model.Spec) string {
	h := sha256.New()
	write := func(s string) { h.Write([]byte(s)); h.Write([]byte{0}) }
	for _, s := range specs {
		write(string(s.Kind))
		write(s.ID)
		write(strings.Join(s.DependsOn, ","))
		write(registry.CanonicalSignature(s.Attrs))
		write(boolStr(s.Protected))
		h.Write([]byte{'\n'})
	}
	return "sha256:" + hex.EncodeToString(h.Sum(nil))
}

func boolStr(b bool) string {
	if b {
		return "1"
	}
	return "0"
}
