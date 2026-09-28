package store

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	"crcontroller/internal/model"
)

func openTestStore(t *testing.T) *Store {
	t.Helper()
	path := filepath.Join(t.TempDir(), "desired.db")
	st, err := Open(path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	return st
}

func TestGenerationBumpsOnlyOnSpecChangeAndRVAlwaysBumps(t *testing.T) {
	ctx := context.Background()
	st := openTestStore(t)

	o, err := st.Create(ctx, CreateInput{
		UID: "u1", Namespace: "ns", Name: "a",
		Spec: map[string]any{"replicas": 1.0},
	})
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if o.Generation != 1 || o.ResourceVer != 1 {
		t.Fatalf("initial gen=%d rv=%d, want 1/1", o.Generation, o.ResourceVer)
	}

	// Same spec: generation stays, rv bumps.
	same, err := st.UpdateSpec(ctx, UpdateSpecInput{
		UID: "u1", ResourceVersion: 1,
		Spec: map[string]any{"replicas": 1.0},
	})
	if err != nil {
		t.Fatalf("same-spec update: %v", err)
	}
	if same.Generation != 1 || same.ResourceVer != 2 {
		t.Fatalf("same spec: gen=%d rv=%d, want 1/2", same.Generation, same.ResourceVer)
	}

	// Changed spec: generation 2, rv 3.
	changed, err := st.UpdateSpec(ctx, UpdateSpecInput{
		UID: "u1", ResourceVersion: 2,
		Spec: map[string]any{"replicas": 3.0},
	})
	if err != nil {
		t.Fatalf("spec update: %v", err)
	}
	if changed.Generation != 2 || changed.ResourceVer != 3 {
		t.Fatalf("changed spec: gen=%d rv=%d, want 2/3", changed.Generation, changed.ResourceVer)
	}

	// Stale rv must conflict rather than overwrite.
	_, err = st.UpdateSpec(ctx, UpdateSpecInput{
		UID: "u1", ResourceVersion: 2,
		Spec: map[string]any{"replicas": 99.0},
	})
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("stale rv: got %v, want ErrConflict", err)
	}
	cur, _ := st.Get(ctx, "u1")
	if cur.Generation != 2 {
		t.Fatalf("conflicting write must not overwrite newer spec: gen=%d", cur.Generation)
	}
}

func TestStatusNeverMovesObservedGenerationBackwards(t *testing.T) {
	ctx := context.Background()
	st := openTestStore(t)

	o, err := st.Create(ctx, CreateInput{
		UID: "u2", Namespace: "ns", Name: "b",
		Spec: map[string]any{"x": 1.0},
	})
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	o, err = st.UpdateSpec(ctx, UpdateSpecInput{
		UID: o.UID, ResourceVersion: o.ResourceVer,
		Spec: map[string]any{"x": 2.0},
	})
	if err != nil {
		t.Fatalf("update: %v", err)
	}
	// Advance status to generation 2.
	o, err = st.UpdateStatus(ctx, StatusPatch{
		UID: o.UID, ResourceVersion: o.ResourceVer,
		ObservedGen: 2, State: "Active",
	})
	if err != nil {
		t.Fatalf("status gen2: %v", err)
	}

	// Old observation (gen1) must be rejected as conflict, not regress status.
	_, err = st.UpdateStatus(ctx, StatusPatch{
		UID: o.UID, ResourceVersion: o.ResourceVer,
		ObservedGen: 1, State: "Stale",
	})
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("stale status: got %v want ErrConflict", err)
	}

	// Future observation (gen99 > generation) refused.
	_, err = st.UpdateStatus(ctx, StatusPatch{
		UID: o.UID, ResourceVersion: o.ResourceVer,
		ObservedGen: 99, State: "Future",
	})
	if !errors.Is(err, ErrRefused) {
		t.Fatalf("future status: got %v want ErrRefused", err)
	}
	cur, _ := st.Get(ctx, o.UID)
	if cur.Status.ObservedGeneration != 2 {
		t.Fatalf("observedGeneration regressed/corrupted: %d", cur.Status.ObservedGeneration)
	}
}

func TestPurgeRefusedWhileFinalizersPresent(t *testing.T) {
	ctx := context.Background()
	st := openTestStore(t)
	o, err := st.Create(ctx, CreateInput{
		UID: "u3", Namespace: "ns", Name: "c", Spec: map[string]any{},
	})
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if _, err := st.SetFinalizers(ctx, SetFinalizersInput{
		UID: o.UID, ResourceVersion: o.ResourceVer,
		Finalizers: []string{model.FinalizerController},
	}); err != nil {
		t.Fatalf("add finalizer: %v", err)
	}
	if err := st.Purge(ctx, o.UID); !errors.Is(err, ErrRefused) {
		t.Fatalf("purge with finalizer: got %v want ErrRefused", err)
	}
	if _, err := st.Delete(ctx, o.UID); err != nil {
		t.Fatalf("mark delete: %v", err)
	}
	// Terminating objects reject new spec updates.
	_, err = st.UpdateSpec(ctx, UpdateSpecInput{
		UID: o.UID, ResourceVersion: o.ResourceVer + 1,
		Spec: map[string]any{"new": true},
	})
	if !errors.Is(err, ErrTerminating) {
		t.Fatalf("spec update while terminating: got %v want ErrTerminating", err)
	}
}
