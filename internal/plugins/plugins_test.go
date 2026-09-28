package plugins

import (
	"context"
	"strings"
	"testing"
	"time"

	"admission/internal/admission"
	"admission/internal/quota"
	"admission/internal/types"
)

func ptr(n int) *int { return &n }

func TestReplicaRangeValidator(t *testing.T) {
	v := &ReplicaRangeValidator{Min: 1, Max: 10}

	dec, err := v.Validate(context.Background(), types.Review{
		Operation: "CREATE", Object: types.Object{Spec: types.Spec{Replicas: ptr(5)}},
	})
	if err != nil || !dec.Allowed {
		t.Fatalf("in-range should allow: dec=%+v err=%v", dec, err)
	}

	// Out of range => clean denial, not an error.
	dec, err = v.Validate(context.Background(), types.Review{
		Operation: "CREATE", Object: types.Object{Spec: types.Spec{Replicas: ptr(11)}},
	})
	if err != nil {
		t.Fatalf("range denial must not be an error: %v", err)
	}
	if dec.Allowed || !strings.Contains(dec.Reason, "outside allowed range") {
		t.Fatalf("expected range denial, got %+v", dec)
	}

	// Missing replicas => InvalidInput error category.
	_, err = v.Validate(context.Background(), types.Review{
		Operation: "CREATE", Object: types.Object{},
	})
	pe, ok := admission.AsPluginError(err)
	if !ok || pe.Category != types.CatInvalidInput {
		t.Fatalf("expected InvalidInput, got %v", err)
	}
}

func TestImmutableValidator(t *testing.T) {
	v := &ImmutableValidator{}
	old := types.Object{Spec: types.Spec{Replicas: ptr(3), CPU: "500m", Memory: "128Mi", Immutable: true}}

	// Unchanged UPDATE allowed.
	cur := old
	dec, err := v.Validate(context.Background(), types.Review{
		Operation: "UPDATE", Object: cur, OldObject: &old,
	})
	if err != nil || !dec.Allowed {
		t.Fatalf("unchanged update: dec=%+v err=%v", dec, err)
	}

	// CPU changed => StateConflict.
	cur = old
	cur.Spec.CPU = "600m"
	_, err = v.Validate(context.Background(), types.Review{
		Operation: "UPDATE", Object: cur, OldObject: &old,
	})
	pe, ok := admission.AsPluginError(err)
	if !ok || pe.Category != types.CatStateConflict || !strings.Contains(pe.Message, "spec.cpu") {
		t.Fatalf("expected StateConflict on spec.cpu, got %v", err)
	}

	// Mutable object: changes allowed.
	oldMut := old
	oldMut.Spec.Immutable = false
	cur = oldMut
	cur.Spec.CPU = "600m"
	dec, _ = v.Validate(context.Background(), types.Review{
		Operation: "UPDATE", Object: cur, OldObject: &oldMut,
	})
	if !dec.Allowed {
		t.Fatalf("mutable object change must be allowed: %+v", dec)
	}
}

func TestQuotaValidatorCategories(t *testing.T) {
	ledger := quota.NewMemoryLedger(1000, 1<<30)
	v := &QuotaValidator{Adapter: ledger}

	ok1, err := v.Validate(context.Background(), types.Review{
		Operation: "CREATE",
		Object:    types.Object{Spec: types.Spec{CPU: "500m", Memory: "128Mi"}},
	})
	if err != nil || !ok1.Allowed {
		t.Fatalf("fitting request: dec=%+v err=%v", ok1, err)
	}

	// Oversize CPU => QuotaExhausted, distinct from denial.
	_, err = v.Validate(context.Background(), types.Review{
		Operation: "CREATE",
		Object:    types.Object{Spec: types.Spec{CPU: "2000m", Memory: "128Mi"}},
	})
	pe, ok := admission.AsPluginError(err)
	if !ok || pe.Category != types.CatQuotaExhausted {
		t.Fatalf("expected QuotaExhausted, got %v", err)
	}
	if !strings.Contains(pe.Message, "cpu") {
		t.Fatalf("message should name cpu: %s", pe.Message)
	}

	// Malformed quantity => InvalidInput, never QuotaExhausted.
	_, err = v.Validate(context.Background(), types.Review{
		Operation: "CREATE",
		Object:    types.Object{Spec: types.Spec{CPU: "huge", Memory: "128Mi"}},
	})
	pe, ok = admission.AsPluginError(err)
	if !ok || pe.Category != types.CatInvalidInput {
		t.Fatalf("expected InvalidInput for bad quantity, got %v", err)
	}
}

func TestMutatorIdempotencyContract(t *testing.T) {
	obj := types.Object{
		Metadata: types.Metadata{Labels: map[string]string{"team": "payments"},
			Annotations: map[string]string{}},
		Spec: types.Spec{CPU: "500m", Memory: "128Mi", Extra: map[string]any{"reservedCPU": float64(500)}},
	}
	obj.Metadata.Annotations["admission.example.com/team"] = "payments"
	obj.Metadata.Annotations["admission.example.com/reserved-cpu-milli"] = "500"

	rr := &ReservedResources{}
	if p, err := rr.Mutate(context.Background(), &obj); err != nil || len(p.Ops) != 0 {
		t.Fatalf("ReservedResources re-entrant patch=%v err=%v, want empty", p.Ops, err)
	}
	ls := &LabelSync{}
	if p, err := ls.Mutate(context.Background(), &obj); err != nil || len(p.Ops) != 0 {
		t.Fatalf("LabelSync re-entrant patch=%v err=%v, want empty", p.Ops, err)
	}
	rd := &ReplicaDefaulter{Default: 3}
	obj.Spec.Replicas = ptr(3)
	if p, err := rd.Mutate(context.Background(), &obj); err != nil || len(p.Ops) != 0 {
		t.Fatalf("ReplicaDefaulter re-entrant patch=%v err=%v, want empty", p.Ops, err)
	}
}

func TestDelayValidatorHonorsDeadline(t *testing.T) {
	v := &DelayValidator{Name_: "slow", Delay: 200 * time.Millisecond}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if _, err := v.Validate(ctx, types.Review{}); err == nil {
		t.Fatal("expected deadline error")
	}
}

func TestFlakyMutatorRecovers(t *testing.T) {
	f := &FlakyMutator{Name_: "f", Failures: 2, AllowedPaths: []string{"/spec/extra/flaky"}}
	for i := 1; i <= 2; i++ {
		_, err := f.Mutate(context.Background(), &types.Object{})
		pe, ok := admission.AsPluginError(err)
		if !ok || pe.Category != types.CatComputeFailure {
			t.Fatalf("call %d expected ComputeFailure, got %v", i, err)
		}
	}
	p, err := f.Mutate(context.Background(), &types.Object{})
	if err != nil || len(p.Ops) != 0 {
		t.Fatalf("third call should recover, patch=%v err=%v", p.Ops, err)
	}
	if f.Calls() != 3 {
		t.Fatalf("calls=%d want 3", f.Calls())
	}
}

func TestRogueMutatorAdvertisesOnlyRoguePrefix(t *testing.T) {
	r := &RogueMutator{Name_: "rogue", Attack: "outside"}
	for _, p := range r.AllowedPrefixes() {
		if p != "/spec/extra/rogue" {
			t.Fatalf("unexpected declared prefix %q", p)
		}
	}
	p, err := r.Mutate(context.Background(), &types.Object{})
	if err != nil || p.Ops[0].Path != "/spec/replicas" {
		t.Fatalf("rogue patch unexpected: %+v err=%v", p, err)
	}
}
