package reconcile_test

import (
	"context"
	"testing"

	"placer/internal/config"
	"placer/internal/logx"
	"placer/internal/model"
	"placer/internal/reconcile"
	"placer/internal/store"
)

func testLoop(t *testing.T, maxRetries int) (*reconcile.Loop, *store.Store) {
	t.Helper()
	st, err := store.Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	cfg := config.Default()
	cfg.MaxRetries = maxRetries
	loop := reconcile.New(st, cfg, logx.New(nil, 0))
	return loop, st
}

func node(id, zone string, cpu int64) model.Node {
	return model.Node{ID: id, Zone: zone, Region: "r", Status: model.NodeReady,
		Capacity: model.Resources{MilliCPU: cpu, Memory: 1e10, Storage: 1e11}}
}

func pend(id string, cpu int64, groups map[string]string) model.Instance {
	return model.Instance{ID: id, State: model.StatePending,
		Request: model.Resources{MilliCPU: cpu, Memory: 1, Storage: 1}, Groups: groups}
}

// TestReconcile_FeasibleBindsInstances: end-to-end successful pass commits
// decisions and records a feasible run.
func TestReconcile_FeasibleBindsInstances(t *testing.T) {
	ctx := context.Background()
	loop, st := testLoop(t, 3)
	_ = st.UpsertNode(ctx, node("a1", "za", 4000))
	_ = st.UpsertNode(ctx, node("b1", "zb", 4000))
	_ = st.UpsertInstance(ctx, pend("p1", 500, nil))

	sum, err := loop.RunOnce(ctx, "run-rec-ok")
	if err != nil {
		t.Fatalf("RunOnce error: %v", err)
	}
	if sum.Status != reconcile.StatusFeasible || len(sum.Decisions) != 1 {
		t.Fatalf("expected one feasible decision, got status=%s decisions=%+v",
			sum.Status, sum.Decisions)
	}
	snap, _ := st.LoadSnapshot(ctx)
	if len(snap.Bound) != 1 || snap.Bound[0].NodeID != "a1" {
		t.Fatalf("expected p1 bound to a1, got %+v", snap.Bound)
	}
	_, status, err := st.RunStatus(ctx, "run-rec-ok")
	if err != nil || status != "feasible" {
		t.Fatalf("stored run status wrong: %s err=%v", status, err)
	}
}

// TestReconcile_ConflictRetriesThenFails verifies the failure lifecycle:
// repeated infeasibility keeps the instance pending (with attempts and a
// last_code) until the retry budget, then flips it to failed — and every
// run row is stored as "conflict", never "feasible".
func TestReconcile_ConflictRetriesThenFails(t *testing.T) {
	ctx := context.Background()
	const maxRetries = 3
	loop, st := testLoop(t, maxRetries)
	_ = st.UpsertNode(ctx, node("a1", "za", 100))
	big := pend("huge", 5000, nil)
	_ = st.UpsertInstance(ctx, big)

	var last reconcile.Summary
	runN := 0
	for runN = 1; runN <= maxRetries; runN++ {
		var err error
		last, err = loop.RunOnce(ctx, "run-rec-conflict-"+itoa(runN))
		if err != nil {
			t.Fatalf("pass %d returned infrastructure error: %v", runN, err)
		}
		if last.Status != reconcile.StatusConflict {
			t.Fatalf("pass %d expected conflict, got %s", runN, last.Status)
		}
		if len(last.Conflicts) != 1 || last.Conflicts[0].Code != model.RejectResources {
			t.Fatalf("pass %d wrong conflicts: %+v", runN, last.Conflicts)
		}
		_, status, _ := st.RunStatus(ctx, "run-rec-conflict-"+itoa(runN))
		if status != "conflict" {
			t.Fatalf("pass %d stored status %s, want conflict", runN, status)
		}
	}

	snap, _ := st.LoadSnapshot(ctx)
	var failed *model.Instance
	for i := range snap.Instances {
		if snap.Instances[i].ID == "huge" {
			failed = &snap.Instances[i]
		}
	}
	if failed == nil {
		t.Fatal("instance missing")
	}
	if failed.State != model.StateFailed {
		t.Fatalf("after %d attempts instance must be failed, got %s", maxRetries, failed.State)
	}
	if failed.Attempts != maxRetries {
		t.Fatalf("attempt counter = %d, want %d", failed.Attempts, maxRetries)
	}
}

// TestReconcile_IdleWhenNothingPending: no pending -> idle, no error and
// no fabricated success placement.
func TestReconcile_IdleWhenNothingPending(t *testing.T) {
	ctx := context.Background()
	loop, st := testLoop(t, 3)
	_ = st.UpsertNode(ctx, node("a1", "za", 4000))
	sum, err := loop.RunOnce(ctx, "run-idle")
	if err != nil {
		t.Fatal(err)
	}
	if sum.Status != reconcile.StatusIdle || len(sum.Decisions) != 0 {
		t.Fatalf("expected idle with no decisions, got %+v", sum)
	}
}

// TestReconcile_InvalidInputIsErrorNotConflict: a malformed world (e.g.
// pending instance referencing nothing but nodes exist — here we simulate a
// scheduler validation error via zero nodes) must surface as an error
// status, never as conflict or success.
func TestReconcile_InvalidInputIsErrorNotConflict(t *testing.T) {
	ctx := context.Background()
	loop, st := testLoop(t, 3)
	// No nodes at all + a pending instance -> scheduler.Plan validation
	// error (nodes list empty).
	_ = st.UpsertInstance(ctx, pend("p1", 100, nil))
	// LoadSnapshot returns zero nodes; Plan validates nodes empty -> error.
	sum, err := loop.RunOnce(ctx, "run-invalid")
	if err == nil {
		t.Fatal("invalid world must return an error")
	}
	if sum.Status != reconcile.StatusError {
		t.Fatalf("expected error status, got %s", sum.Status)
	}
	_, status, _ := st.RunStatus(ctx, "run-invalid")
	if status != "error" {
		t.Fatalf("stored status must be error, got %s", status)
	}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [12]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}
