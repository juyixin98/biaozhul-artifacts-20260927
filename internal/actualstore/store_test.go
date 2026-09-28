package actualstore

import (
	"errors"
	"path/filepath"
	"testing"
)

func openTestActual(t *testing.T) *Store {
	t.Helper()
	st, err := Open(filepath.Join(t.TempDir(), "actual.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	return st
}

func TestCreateIsIdempotentPerOwner(t *testing.T) {
	st := openTestActual(t)

	first, err := st.Create(CreateInput{
		ID: "r-1", OwnerUID: "owner-1", Generation: 1,
		SpecHash: "h1", Spec: map[string]any{"v": 1.0},
	})
	if err != nil {
		t.Fatalf("first create: %v", err)
	}
	if first.Version != 1 {
		t.Fatalf("initial version = %d, want 1", first.Version)
	}

	// A second create attempt for the same owner must NOT make a row; it
	// returns the existing resource with ErrAlreadyOwned.
	second, err := st.Create(CreateInput{
		ID: "r-2", OwnerUID: "owner-1", Generation: 1,
		SpecHash: "h1", Spec: map[string]any{"v": 1.0},
	})
	if !errors.Is(err, ErrAlreadyOwned) {
		t.Fatalf("duplicate create: got %v want ErrAlreadyOwned", err)
	}
	if second.ID != "r-1" {
		t.Fatalf("duplicate create must return existing row, got id=%s", second.ID)
	}
	rows, _ := st.List(10)
	if len(rows) != 1 {
		t.Fatalf("expected exactly 1 physical resource, got %d", len(rows))
	}
}

func TestConditionalUpdateAndDelete(t *testing.T) {
	st := openTestActual(t)
	r, err := st.Create(CreateInput{
		ID: "r-9", OwnerUID: "owner-9", Generation: 1,
		SpecHash: "h1", Spec: map[string]any{"v": 1.0},
	})
	if err != nil {
		t.Fatalf("create: %v", err)
	}

	// Stale expected version must conflict without mutating.
	_, err = st.Update(UpdateInput{
		ID: "r-9", ExpectedVer: r.Version - 1, Generation: 2,
		SpecHash: "h2", Spec: map[string]any{"v": 2.0},
	})
	if !errors.Is(err, ErrVersionConflict) {
		t.Fatalf("stale update: got %v want ErrVersionConflict", err)
	}
	cur, _ := st.Get("r-9")
	if cur.Version != 1 || cur.Generation != 1 {
		t.Fatalf("conflicting update mutated row: ver=%d gen=%d", cur.Version, cur.Generation)
	}

	// Correct expected version advances version.
	updated, err := st.Update(UpdateInput{
		ID: "r-9", ExpectedVer: 1, Generation: 2,
		SpecHash: "h2", Spec: map[string]any{"v": 2.0},
	})
	if err != nil {
		t.Fatalf("update: %v", err)
	}
	if updated.Version != 2 || updated.Generation != 2 {
		t.Fatalf("updated ver=%d gen=%d, want 2/2", updated.Version, updated.Generation)
	}

	// Delete is idempotent.
	if err := st.Delete("r-9", 0); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if err := st.Delete("r-9", 0); !errors.Is(err, ErrNotFound) {
		t.Fatalf("second delete: got %v want ErrNotFound", err)
	}
}
