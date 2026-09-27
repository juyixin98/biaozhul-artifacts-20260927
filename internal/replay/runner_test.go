package replay

import (
	"context"
	"path/filepath"
	"strings"
	"testing"

	"pathvector/internal/config"
	"pathvector/internal/engine"
	"pathvector/internal/ierr"
	"pathvector/internal/store"
)

func fixturePath(name string) string {
	return filepath.Join("..", "..", "testdata", "fixtures", name+".json")
}

func TestExecuteConvergedPersists(t *testing.T) {
	ctx := context.Background()
	st, err := store.Open(ctx, ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()

	r := NewRunner(st)
	cfg, err := config.LoadFile(fixturePath("multi_exit"))
	if err != nil {
		t.Fatal(err)
	}
	res, err := r.ExecuteConfig(ctx, cfg, nil)
	if err != nil {
		t.Fatal(err)
	}
	if res.RunID == "" || !strings.HasPrefix(res.RunID, "run-") {
		t.Fatalf("run id = %q", res.RunID)
	}
	if res.Report.Status != engine.StatusConverged {
		t.Fatalf("status=%s", res.Report.Status)
	}
	rec, err := st.GetRun(ctx, res.RunID)
	if err != nil {
		t.Fatalf("run not persisted: %v", err)
	}
	if rec.Status != "converged" {
		t.Fatalf("stored status=%s", rec.Status)
	}
}

func TestExecuteInvalidInputBeforePersistence(t *testing.T) {
	ctx := context.Background()
	st, _ := store.Open(ctx, ":memory:")
	defer st.Close()
	r := NewRunner(st)

	_, err := r.Execute(ctx, []byte(`{"topology":{"nodes":[],"links":[]}}`))
	if err == nil || !ierr.Is(err, ierr.KindInvalidInput) {
		t.Fatalf("err=%v want invalid_input", err)
	}
	rows, _ := st.ListRuns(ctx, 10)
	if len(rows) != 0 {
		t.Fatalf("invalid input must not create a run, got %d", len(rows))
	}
}

func TestExecuteStateConflictPersistedWithKind(t *testing.T) {
	ctx := context.Background()
	st, _ := store.Open(ctx, ":memory:")
	defer st.Close()
	r := NewRunner(st)

	cfg, err := config.LoadFile(fixturePath("unknown_withdraw"))
	if err != nil {
		t.Fatal(err)
	}
	res, err := r.ExecuteConfig(ctx, cfg, nil)
	if err == nil || !ierr.Is(err, ierr.KindStateConflict) {
		t.Fatalf("err=%v want state_conflict", err)
	}
	if res == nil || res.RunID == "" {
		t.Fatal("result with run id must accompany the failure")
	}
	rec, gerr := st.GetRun(ctx, res.RunID)
	if gerr != nil {
		t.Fatalf("failed run must be archived: %v", gerr)
	}
	if rec.ErrorKind != "state_conflict" {
		t.Fatalf("archived error_kind=%q", rec.ErrorKind)
	}
}

// failingStore models a storage I/O failure; it must surface as
// computation_failed while the engine result itself stays intact.
type failingStore struct{}

func (failingStore) SaveRun(_ context.Context, _ store.SaveInput) error {
	return ierr.New(ierr.KindComputationFailed, "store.SaveRun", "disk on fire")
}

func TestStorageFailureClassified(t *testing.T) {
	ctx := context.Background()
	cfg, err := config.LoadFile(fixturePath("multi_exit"))
	if err != nil {
		t.Fatal(err)
	}
	r := NewRunner(failingStore{})
	res, err := r.ExecuteConfig(ctx, cfg, nil)
	if err == nil || !ierr.Is(err, ierr.KindComputationFailed) {
		t.Fatalf("err=%v want computation_failed", err)
	}
	if res == nil || res.Report == nil || res.Report.Status != engine.StatusConverged {
		t.Fatal("engine converged result must still be returned")
	}
}

func TestDeterministicRunIDsUnique(t *testing.T) {
	a, b := GenerateRunID(), GenerateRunID()
	if a == b {
		t.Fatal("run ids collided")
	}
}
