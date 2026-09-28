package store

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"resourcecontroller/internal/model"
)

func openTestStore(t *testing.T) *Store {
	t.Helper()
	dsn := "file:" + filepath.Join(t.TempDir(), "test.db") + "?_pragma=busy_timeout(2000)"
	st, err := Open(context.Background(), dsn)
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	return st
}

func sampleWidget(name string) *model.Widget {
	now := time.Now().UTC()
	return &model.Widget{
		Meta: model.ObjectMeta{
			Name: name, UID: "uid-" + name, Generation: 1, ResourceVersion: 1,
			CreatedAt: now, UpdatedAt: now,
		},
		Spec:   model.WidgetSpec{Replicas: 2, Color: "blue", SecretToken: "tok-abcdef-1"},
		Status: model.WidgetStatus{Phase: model.PhasePending},
	}
}

func TestCreateAndGet(t *testing.T) {
	st := openTestStore(t)
	ctx := context.Background()
	w := sampleWidget("w1")
	if err := st.Create(ctx, w); err != nil {
		t.Fatalf("create: %v", err)
	}
	if err := st.Create(ctx, sampleWidget("w1")); !errors.Is(err, ErrAlreadyExists) {
		t.Fatalf("duplicate create: want ErrAlreadyExists, got %v", err)
	}
	got, err := st.Get(ctx, "w1")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if got.Spec.Color != "blue" || got.Meta.Generation != 1 {
		t.Fatalf("unexpected row: %+v", got)
	}
	if _, err := st.Get(ctx, "missing"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing get: want ErrNotFound, got %v", err)
	}
}

func TestSaveSpecCASConflict(t *testing.T) {
	st := openTestStore(t)
	ctx := context.Background()
	w := sampleWidget("cas")
	if err := st.Create(ctx, w); err != nil {
		t.Fatal(err)
	}

	// A stale writer must be rejected, not merge over the newer row.
	stale := *w
	stale.Spec = model.WidgetSpec{Replicas: 99, Color: "stale"}
	stale.Meta.Generation = 7
	if _, err := st.SaveSpec(ctx, &stale, 999); !errors.Is(err, ErrVersionConflict) {
		t.Fatalf("stale CAS: want conflict, got %v", err)
	}

	// The current writer bumps the generation and resource version.
	cur := *w
	cur.Spec = model.WidgetSpec{Replicas: 3, Color: "green"}
	cur.Meta.Generation = 2
	saved, err := st.SaveSpec(ctx, &cur, 1)
	if err != nil {
		t.Fatalf("save spec: %v", err)
	}
	if saved.Meta.ResourceVersion != 2 {
		t.Fatalf("resource version: want 2, got %d", saved.Meta.ResourceVersion)
	}
	got, _ := st.Get(ctx, "cas")
	if got.Spec.Replicas != 3 || got.Meta.Generation != 2 {
		t.Fatalf("newer spec not persisted: %+v", got.Spec)
	}

	// The stale writer trying again with the old rv still fails.
	if _, err := st.SaveSpec(ctx, &stale, 1); !errors.Is(err, ErrVersionConflict) {
		t.Fatalf("second stale CAS: want conflict, got %v", err)
	}
}

func TestSaveStatusIsMonotonic(t *testing.T) {
	st := openTestStore(t)
	ctx := context.Background()
	w := sampleWidget("mono")
	if err := st.Create(ctx, w); err != nil {
		t.Fatal(err)
	}
	// Controller confirms generation 3.
	adv := model.WidgetStatus{Phase: model.PhaseReady, ObservedGeneration: 3, ReconciledGeneration: 3}
	if _, err := st.SaveStatus(ctx, "mono", adv, 1); err != nil {
		t.Fatal(err)
	}
	// A late, stale observation for generation 1 must not move progress back.
	old := model.WidgetStatus{Phase: model.PhaseSyncing, ObservedGeneration: 1, ReconciledGeneration: 1}
	saved, err := st.SaveStatus(ctx, "mono", old, 2)
	if err != nil {
		t.Fatal(err)
	}
	if saved.Status.ObservedGeneration != 3 || saved.Status.ReconciledGeneration != 3 {
		t.Fatalf("progress regressed: obs=%d rec=%d",
			saved.Status.ObservedGeneration, saved.Status.ReconciledGeneration)
	}
}

func TestFinalizerLifecycleAndDelete(t *testing.T) {
	st := openTestStore(t)
	ctx := context.Background()
	w := sampleWidget("fin")
	if err := st.Create(ctx, w); err != nil {
		t.Fatal(err)
	}
	got, err := st.AddFinalizer(ctx, "fin", "test/finalizer", 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Meta.Finalizers) != 1 || got.Meta.Finalizers[0] != "test/finalizer" {
		t.Fatalf("finalizer not stored: %#v", got.Meta.Finalizers)
	}
	// Idempotent: adding again must not bump the version or duplicate.
	again, err := st.AddFinalizer(ctx, "fin", "test/finalizer", got.Meta.ResourceVersion)
	if err != nil {
		t.Fatal(err)
	}
	if len(again.Meta.Finalizers) != 1 || again.Meta.ResourceVersion != got.Meta.ResourceVersion {
		t.Fatalf("finalizer add not idempotent: %#v rv=%d", again.Meta.Finalizers, again.Meta.ResourceVersion)
	}
	// Delete with a stale rv is rejected.
	if err := st.Delete(ctx, "fin", 1); !errors.Is(err, ErrVersionConflict) {
		t.Fatalf("stale delete: want conflict, got %v", err)
	}
	if err := st.Delete(ctx, "fin", again.Meta.ResourceVersion); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if exists, _ := st.QueryExists(ctx, "fin"); exists {
		t.Fatal("row still present after delete")
	}
}

func TestListPending(t *testing.T) {
	st := openTestStore(t)
	ctx := context.Background()
	mk := func(name string, gen, rec int64, deleting bool) {
		w := sampleWidget(name)
		w.Meta.Generation = gen
		if deleting {
			ts := time.Now().UTC()
			w.Meta.DeletionTimestamp = &ts
			w.Meta.Finalizers = []string{"f"}
		}
		w.Status.ReconciledGeneration = rec
		w.Status.ObservedGeneration = rec
		if err := st.Create(ctx, w); err != nil {
			t.Fatal(err)
		}
	}
	mk("ready", 2, 2, false)
	mk("lagging", 3, 2, false)
	mk("deleting", 1, 1, true)
	pending, err := st.ListPending(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, p := range pending {
		names = append(names, p.Meta.Name)
	}
	if len(names) != 2 || names[0] != "deleting" || names[1] != "lagging" {
		t.Fatalf("pending = %v, want [deleting lagging]", names)
	}
}
