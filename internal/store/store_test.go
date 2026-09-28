package store_test

import (
	"context"
	"testing"

	"placer/internal/model"
	"placer/internal/store"
)

func openTestStore(t *testing.T) *store.Store {
	t.Helper()
	st, err := store.Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	return st
}

func sampleNode(id, zone string) model.Node {
	return model.Node{ID: id, Zone: zone, Region: "r", Status: model.NodeReady,
		Capacity: model.Resources{MilliCPU: 2000, Memory: 4e9, Storage: 1e11}}
}

func TestStore_RoundTripSnapshot(t *testing.T) {
	ctx := context.Background()
	st := openTestStore(t)

	if err := st.UpsertNode(ctx, sampleNode("a1", "za")); err != nil {
		t.Fatal(err)
	}
	pending := model.Instance{ID: "p1", State: model.StatePending,
		Request: model.Resources{MilliCPU: 500, Memory: 1e8, Storage: 1e8}}
	bound := model.Instance{ID: "b1", State: model.StateBound, NodeID: "a1",
		Request: model.Resources{MilliCPU: 300, Memory: 1, Storage: 1},
		Groups:  map[string]string{"app": "w"}}
	if err := st.UpsertInstance(ctx, pending); err != nil {
		t.Fatal(err)
	}
	if err := st.UpsertInstance(ctx, bound); err != nil {
		t.Fatal(err)
	}
	if err := st.SetPolicy(ctx, model.Policy{Groups: []model.GroupRule{
		{Group: "app", Mode: model.ModeHard, TopologyKey: "zone"},
	}}); err != nil {
		t.Fatal(err)
	}

	snap, err := st.LoadSnapshot(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(snap.Nodes) != 1 || snap.Nodes[0].ID != "a1" || snap.Nodes[0].Capacity.MilliCPU != 2000 {
		t.Fatalf("node round-trip wrong: %+v", snap.Nodes)
	}
	if len(snap.Pending) != 1 || snap.Pending[0].ID != "p1" {
		t.Fatalf("pending wrong: %+v", snap.Pending)
	}
	if len(snap.Bound) != 1 || snap.Bound[0].NodeID != "a1" || snap.Bound[0].Groups["app"] != "w" {
		t.Fatalf("bound wrong: %+v", snap.Bound)
	}
	if len(snap.Policy.Groups) != 1 {
		t.Fatalf("policy wrong: %+v", snap.Policy)
	}
}

// TestStore_CommitIsAtomic verifies rule-3 at the persistence boundary:
// a decision whose instance is no longer pending aborts the WHOLE
// transaction, so earlier decisions in the same batch are not committed.
func TestStore_CommitIsAtomic(t *testing.T) {
	ctx := context.Background()
	st := openTestStore(t)
	if err := st.UpsertNode(ctx, sampleNode("a1", "za")); err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{"p1", "p2"} {
		if err := st.UpsertInstance(ctx, model.Instance{ID: id, State: model.StatePending,
			Request: model.Resources{MilliCPU: 1, Memory: 1, Storage: 1}}); err != nil {
			t.Fatal(err)
		}
	}

	// p2 disappears concurrently (deleted) before commit; the p1 decision
	// in the same transaction must roll back.
	if err := st.DeleteInstance(ctx, "p2"); err != nil {
		t.Fatal(err)
	}
	err := st.CommitBindings(ctx, "run-atomic", []model.Decision{
		{InstanceID: "p1", NodeID: "a1"},
		{InstanceID: "p2", NodeID: "a1"},
	})
	if err == nil {
		t.Fatal("commit must fail when a decision target is not pending")
	}
	snap, _ := st.LoadSnapshot(ctx)
	for _, in := range snap.Instances {
		if in.ID == "p1" && in.State != model.StatePending {
			t.Fatalf("p1 must remain pending after aborted tx, got %s", in.State)
		}
	}
}

// TestStore_RetryAndTerminalFailure checks the explicit failure lifecycle.
func TestStore_RetryAndTerminalFailure(t *testing.T) {
	ctx := context.Background()
	st := openTestStore(t)
	if err := st.UpsertInstance(ctx, model.Instance{ID: "p1", State: model.StatePending,
		Request: model.Resources{MilliCPU: 1, Memory: 1, Storage: 1}}); err != nil {
		t.Fatal(err)
	}
	cf := []model.Conflict{{InstanceID: "p1", Code: model.RejectResources, Detail: "full"}}

	if err := st.MarkFailed(ctx, "run-r1", cf, false); err != nil {
		t.Fatal(err)
	}
	n, err := st.Attempts(ctx, "p1")
	if err != nil || n != 1 {
		t.Fatalf("attempts=%d err=%v, want 1", n, err)
	}
	snap, _ := st.LoadSnapshot(ctx)
	if snap.Pending[0].State != model.StatePending {
		t.Fatal("non-terminal mark must keep instance pending")
	}

	if err := st.MarkFailed(ctx, "run-r2", cf, true); err != nil {
		t.Fatal(err)
	}
	snap, _ = st.LoadSnapshot(ctx)
	var found *model.Instance
	for i := range snap.Instances {
		if snap.Instances[i].ID == "p1" {
			found = &snap.Instances[i]
		}
	}
	if found == nil || found.State != model.StateFailed {
		t.Fatalf("terminal mark must flip to failed, got %+v", found)
	}
}

// TestStore_RunsAndEvents ensures run identity and the durable event
// stream correlate and are retrievable.
func TestStore_RunsAndEvents(t *testing.T) {
	ctx := context.Background()
	st := openTestStore(t)
	if err := st.UpsertNode(ctx, sampleNode("a1", "za")); err != nil {
		t.Fatal(err)
	}
	if err := st.UpsertInstance(ctx, model.Instance{ID: "p1", State: model.StatePending,
		Request: model.Resources{MilliCPU: 1, Memory: 1, Storage: 1}}); err != nil {
		t.Fatal(err)
	}
	if err := st.SaveRun(ctx, "run-x", "plan", "feasible",
		map[string]string{"in": "..."}, map[string]string{"out": "..."}); err != nil {
		t.Fatal(err)
	}
	if err := st.CommitBindings(ctx, "run-x", []model.Decision{{InstanceID: "p1", NodeID: "a1"}}); err != nil {
		t.Fatal(err)
	}
	kind, status, err := st.RunStatus(ctx, "run-x")
	if err != nil || kind != "plan" || status != "feasible" {
		t.Fatalf("run lookup wrong: kind=%s status=%s err=%v", kind, status, err)
	}
	evs, err := st.Events(ctx, "run-x")
	if err != nil || len(evs) != 1 {
		t.Fatalf("events wrong: %+v err=%v", evs, err)
	}
	if evs[0]["kind"] != "bindings_committed" {
		t.Fatalf("event kind wrong: %+v", evs[0])
	}
}

// TestStore_DeleteNodeWithBoundRejected ensures unsafe inventory mutation
// is refused rather than orphaning occupants.
func TestStore_DeleteNodeWithBoundRejected(t *testing.T) {
	ctx := context.Background()
	st := openTestStore(t)
	_ = st.UpsertNode(ctx, sampleNode("a1", "za"))
	_ = st.UpsertInstance(ctx, model.Instance{ID: "b1", State: model.StateBound,
		NodeID: "a1", Request: model.Resources{MilliCPU: 1, Memory: 1, Storage: 1}})
	if err := st.DeleteNode(ctx, "a1"); err == nil {
		t.Fatal("deleting a node with bound instances must be rejected")
	}
}
