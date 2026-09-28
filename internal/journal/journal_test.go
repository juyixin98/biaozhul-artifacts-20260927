package journal

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"infraplanner/internal/model"
)

// TestDurability_AcrossStoreReopen simulates a process restart: write a run,
// an inflight op and evidence with one Store, close it, reopen the same file
// with a fresh Store and assert every byte survived. This is the durability
// property cross-process resume depends on.
func TestDurability_AcrossStoreReopen(t *testing.T) {
	dir := t.TempDir()
	dsn := filepath.Join(dir, "j.db")
	ctx := context.Background()

	s1, err := Open(dsn)
	if err != nil {
		t.Fatal(err)
	}
	spec := []model.Desired{{Kind: model.KindVPC, Name: "main",
		Attrs: map[string]string{"cidr": "10/8", "region": "east"}}}
	if err := s1.CreateRun(ctx, "run-x", spec, []model.Key{{Kind: model.KindVPC, Name: "old"}}); err != nil {
		t.Fatal(err)
	}
	if err := s1.SetPlan(ctx, "run-x", "fp-123", []byte(`{"operations":[]}`)); err != nil {
		t.Fatal(err)
	}
	if err := s1.SetRunState(ctx, "run-x", model.RunApplying, nil); err != nil {
		t.Fatal(err)
	}
	if err := s1.UpsertOp(ctx, "run-x", OpRow{
		Seq: 1, Type: "create", Key: model.Key{Kind: model.KindVPC, Name: "main"},
		State: model.OpInflight, Attempts: 1,
	}); err != nil {
		t.Fatal(err)
	}
	if err := s1.AddEvidence(ctx, Evidence{
		RunID: "run-x", Seq: 1, Attempt: 1, Kind: "request",
		At:   time.Now().UTC(),
		Body: `{"op":"create"}`,
	}); err != nil {
		t.Fatal(err)
	}
	if err := s1.Close(); err != nil {
		t.Fatal(err)
	}

	// Reopen with a brand new connection, as a recovered process would.
	s2, err := Open(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()

	run, err := s2.GetRun(ctx, "run-x")
	if err != nil {
		t.Fatal(err)
	}
	if run == nil {
		t.Fatal("run missing after reopen")
	}
	if run.State != model.RunApplying || run.Fingerprint != "fp-123" {
		t.Fatalf("run = state %s fp %s, want applying/fp-123", run.State, run.Fingerprint)
	}
	if len(run.Spec) != 1 || run.Spec[0].Name != "main" {
		t.Fatalf("spec lost: %+v", run.Spec)
	}
	if len(run.Releases) != 1 || run.Releases[0].Name != "old" {
		t.Fatalf("releases lost: %+v", run.Releases)
	}

	op, err := s2.GetOp(ctx, "run-x", 1)
	if err != nil {
		t.Fatal(err)
	}
	if op.State != model.OpInflight || op.Attempts != 1 {
		t.Fatalf("op = %+v, want inflight/1", op)
	}

	ev, err := s2.ListEvidence(ctx, "run-x")
	if err != nil {
		t.Fatal(err)
	}
	if len(ev) != 1 || ev[0].Kind != "request" || ev[0].Body != `{"op":"create"}` {
		t.Fatalf("evidence lost: %+v", ev)
	}
}

func TestRun_TerminalErrorPersisted(t *testing.T) {
	s, err := Open(":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	ctx := context.Background()
	if err := s.CreateRun(ctx, "r1", nil, nil); err != nil {
		t.Fatal(err)
	}
	term := model.E(model.CatExhaustion, "capacity_full", "full")
	if err := s.SetRunState(ctx, "r1", model.RunFailed, term); err != nil {
		t.Fatal(err)
	}
	run, _ := s.GetRun(ctx, "r1")
	if run.Err == nil || run.Err.Category != model.CatExhaustion || run.Err.Code != "capacity_full" {
		t.Fatalf("terminal error not persisted: %+v", run.Err)
	}
}

func TestCountOpsInState(t *testing.T) {
	s, err := Open(":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	ctx := context.Background()
	_ = s.CreateRun(ctx, "r", nil, nil)
	for i, st := range []model.OpState{model.OpSucceeded, model.OpSucceeded, model.OpInflight, model.OpPending} {
		_ = s.UpsertOp(ctx, "r", OpRow{Seq: i + 1, Type: "create",
			Key:   model.Key{Kind: model.KindVPC, Name: "x"},
			State: st})
	}
	counts, err := s.CountOpsInState(ctx, "r", model.OpSucceeded, model.OpInflight)
	if err != nil {
		t.Fatal(err)
	}
	if counts[model.OpSucceeded] != 2 || counts[model.OpInflight] != 1 {
		t.Fatalf("counts = %v, want succeeded=2 inflight=1", counts)
	}
}
