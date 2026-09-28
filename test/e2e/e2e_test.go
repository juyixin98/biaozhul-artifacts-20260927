// Package e2e contains integration tests that exercise the real binary
// components together: fixture loading, SQLite persistence, reconciliation
// loop, scheduler and the durable run/event log — never a stub.
package e2e

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"placer/internal/config"
	"placer/internal/logx"
	"placer/internal/model"
	"placer/internal/reconcile"
	"placer/internal/store"
)

// fixture mirrors cmd/placer fixture loading but lives in-test so the e2e
// suite stays self-contained.
type fixture struct {
	Name      string           `json:"name"`
	Nodes     []model.Node     `json:"nodes"`
	Instances []model.Instance `json:"instances"`
	Policy    model.Policy     `json:"policy"`
}

func loadFixture(t *testing.T, name string) fixture {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("..", "..", "test", "testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	var fx fixture
	if err := json.Unmarshal(data, &fx); err != nil {
		t.Fatal(err)
	}
	return fx
}

// TestE2E_SkewedFixture_Reconcile places a pending web instance against the
// committed skewed cluster and asserts it lands in the empty zone, with the
// decision durable in the run/event log.
func TestE2E_SkewedFixture_Reconcile(t *testing.T) {
	ctx := context.Background()
	fx := loadFixture(t, "skewed.json")

	st, err := store.Open(ctx, ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()

	for i := range fx.Nodes {
		if err := st.UpsertNode(ctx, fx.Nodes[i]); err != nil {
			t.Fatal(err)
		}
	}
	for i := range fx.Instances {
		if err := st.UpsertInstance(ctx, fx.Instances[i]); err != nil {
			t.Fatal(err)
		}
	}
	if err := st.SetPolicy(ctx, fx.Policy); err != nil {
		t.Fatal(err)
	}

	// Add a pending instance that must balance into zone-c.
	pending := model.Instance{ID: "web-e2e", State: model.StatePending,
		Request: model.Resources{MilliCPU: 500, Memory: 800000000, Storage: 1e9},
		Groups:  map[string]string{"app": "web"}}
	if err := st.UpsertInstance(ctx, pending); err != nil {
		t.Fatal(err)
	}

	cfg := config.Default()
	loop := reconcile.New(st, cfg, logx.New(nil, 0))
	sum, err := loop.RunOnce(ctx, "run-e2e-skewed")
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if sum.Status != reconcile.StatusFeasible {
		t.Fatalf("expected feasible, status=%s conflicts=%+v", sum.Status, sum.Conflicts)
	}
	nodeOf := ""
	for _, d := range sum.Decisions {
		if d.InstanceID == "web-e2e" {
			nodeOf = d.NodeID
		}
	}
	if nodeOf != "n-c-1" {
		t.Fatalf("web-e2e must land in empty zone-c node n-c-1, got %q", nodeOf)
	}

	// Durable verification.
	snap, err := st.LoadSnapshot(ctx)
	if err != nil {
		t.Fatal(err)
	}
	bound := map[string]string{}
	for _, b := range snap.Bound {
		bound[b.InstanceID] = b.NodeID
	}
	if bound["web-e2e"] != "n-c-1" {
		t.Fatalf("durable binding wrong: %+v", bound)
	}
	evs, err := st.Events(ctx, "run-e2e-skewed")
	if err != nil || len(evs) != 1 {
		t.Fatalf("durable event wrong: %+v err=%v", evs, err)
	}
}

// TestE2E_TightFixture_ConflictLifecycle loads the tight cluster,
// schedules three oversized instances that cannot fit, and verifies the
// reconcile loop marks them failed after the retry budget while recording
// conflict (never feasible) runs throughout.
func TestE2E_TightFixture_ConflictLifecycle(t *testing.T) {
	ctx := context.Background()
	fx := loadFixture(t, "tight.json")

	st, err := store.Open(ctx, ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	for i := range fx.Nodes {
		if err := st.UpsertNode(ctx, fx.Nodes[i]); err != nil {
			t.Fatal(err)
		}
	}

	cfg := config.Default()
	cfg.MaxRetries = 2
	loop := reconcile.New(st, cfg, logx.New(nil, 0))

	for k := 0; k < 3; k++ {
		id := "big-" + string(rune('0'+k))
		if err := st.UpsertInstance(ctx, model.Instance{
			ID: id, State: model.StatePending,
			Request: model.Resources{MilliCPU: 3000, Memory: 1, Storage: 1},
		}); err != nil {
			t.Fatal(err)
		}
	}

	for pass := 1; pass <= 2; pass++ {
		sum, err := loop.RunOnce(ctx, "run-e2e-tight-"+string(rune('0'+pass)))
		if err != nil {
			t.Fatalf("pass %d: %v", pass, err)
		}
		if sum.Status != reconcile.StatusConflict {
			t.Fatalf("pass %d expected conflict, got %s", pass, sum.Status)
		}
	}
	snap, _ := st.LoadSnapshot(ctx)
	for _, in := range snap.Instances {
		if in.State != model.StateFailed {
			t.Fatalf("instance %s should be failed, got %s (attempts=%d)",
				in.ID, in.State, in.Attempts)
		}
	}
}
