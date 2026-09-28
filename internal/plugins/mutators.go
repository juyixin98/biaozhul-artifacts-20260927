package plugins

import (
	"context"
	"time"

	"admission/internal/model"
)

// DefaultsCatalog maps kind -> spec key -> default value. Loaded from a local
// JSON file (config.DefaultsFile); no remote configuration source exists.
type DefaultsCatalog map[string]map[string]any

// DefaultsMutator fills missing spec values from the catalog. It only ever
// emits "add" patches, so a value already present is left untouched and a
// re-run on the completed document is a no-op (idempotent).
type DefaultsMutator struct {
	Catalog  DefaultsCatalog
	TimeoutD time.Duration
	Policy   FailPolicy
}

func (m *DefaultsMutator) Name() string           { return "defaults" }
func (m *DefaultsMutator) Timeout() time.Duration { return m.TimeoutD }
func (m *DefaultsMutator) OnError() FailPolicy    { return m.Policy }

// DeclaredPaths reports a path per catalog key of every kind, which keeps the
// declared set tight and auditable.
func (m *DefaultsMutator) DeclaredPaths() []string {
	var paths []string
	for _, keys := range m.Catalog {
		for k := range keys {
			paths = append(paths, "/spec/"+escapeToken(k))
		}
	}
	return paths
}

func (m *DefaultsMutator) Mutate(_ context.Context, in Input) ([]model.PatchOp, error) {
	res, err := model.ResourceFromMap(in.Doc)
	if err != nil {
		return nil, Fail(model.ReasonInvalidInput, "parse resource: %v", err)
	}
	defaults, ok := m.Catalog[res.Kind]
	if !ok {
		return nil, nil // unknown kinds have no defaults to add
	}
	var ops []model.PatchOp
	for k, v := range defaults {
		if _, present := res.Spec[k]; present {
			continue
		}
		ops = append(ops, model.PatchOp{Op: "add", Path: "/spec/" + escapeToken(k), Value: v})
	}
	return ops, nil
}

// CapacityMutator derives spec.capacity from replicas (capacity = replicas *
// perReplica). Placed after DefaultsMutator in the ordered chain so replicas
// is guaranteed present; the two plugins *interact*: changing the per-replica
// default changes what capacity this plugin writes.
type CapacityMutator struct {
	PerReplica int64
	TimeoutD   time.Duration
	Policy     FailPolicy
}

func (m *CapacityMutator) Name() string           { return "capacity" }
func (m *CapacityMutator) Timeout() time.Duration { return m.TimeoutD }
func (m *CapacityMutator) OnError() FailPolicy    { return m.Policy }
func (m *CapacityMutator) DeclaredPaths() []string {
	return []string{"/spec/capacity"}
}

func (m *CapacityMutator) Mutate(_ context.Context, in Input) ([]model.PatchOp, error) {
	res, err := model.ResourceFromMap(in.Doc)
	if err != nil {
		return nil, Fail(model.ReasonInvalidInput, "parse resource: %v", err)
	}
	raw, ok := res.Spec["replicas"]
	if !ok {
		return nil, Fail(model.ReasonComputeFailure, "capacity requires spec.replicas to be set first (chain ordering error)")
	}
	replicas, ok := toInt(raw)
	if !ok {
		return nil, Fail(model.ReasonInvalidInput, "spec.replicas must be an integer, got %v", raw)
	}
	want := replicas * m.PerReplica
	if have, present := res.Spec["capacity"]; present {
		// Re-entrant run: only patch when a drift exists.
		if n, ok := toInt(have); ok && n == want {
			return nil, nil
		}
		return []model.PatchOp{{Op: "replace", Path: "/spec/capacity", Value: want}}, nil
	}
	return []model.PatchOp{{Op: "add", Path: "/spec/capacity", Value: want}}, nil
}

// StampMutator records the admission UID on the object so the persisted
// resource can always be correlated with its request and audit trail.
type StampMutator struct {
	TimeoutD time.Duration
	Policy   FailPolicy
}

func (m *StampMutator) Name() string           { return "stamp-uid" }
func (m *StampMutator) Timeout() time.Duration { return m.TimeoutD }
func (m *StampMutator) OnError() FailPolicy    { return m.Policy }
func (m *StampMutator) DeclaredPaths() []string {
	return []string{"/metadata/annotations/*"}
}

func (m *StampMutator) Mutate(_ context.Context, in Input) ([]model.PatchOp, error) {
	if in.Request.UID == "" {
		return nil, Fail(model.ReasonInvalidInput, "request uid is empty")
	}
	path := "/metadata/annotations/admission.uid"
	res, err := model.ResourceFromMap(in.Doc)
	if err != nil {
		return nil, Fail(model.ReasonInvalidInput, "parse resource: %v", err)
	}
	if v := res.Metadata.Annotations["admission.uid"]; v == in.Request.UID {
		return nil, nil // already stamped: idempotent
	}
	return []model.PatchOp{{Op: "add", Path: path, Value: in.Request.UID}}, nil
}

func toInt(v any) (int64, bool) {
	switch n := v.(type) {
	case int:
		return int64(n), true
	case int64:
		return n, true
	case float64:
		if n == float64(int64(n)) {
			return int64(n), true
		}
	}
	return 0, false
}

func escapeToken(t string) string {
	// Spec keys in our catalog are simple, but honor pointer escaping anyway.
	out := make([]byte, 0, len(t))
	for i := 0; i < len(t); i++ {
		switch t[i] {
		case '~':
			out = append(out, '~', '0')
		case '/':
			out = append(out, '~', '1')
		default:
			out = append(out, t[i])
		}
	}
	return string(out)
}

var _ Mutator = (*DefaultsMutator)(nil)
var _ Mutator = (*CapacityMutator)(nil)
var _ Mutator = (*StampMutator)(nil)
