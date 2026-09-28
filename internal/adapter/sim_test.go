package adapter_test

import (
	"context"
	"path/filepath"
	"testing"

	"rollctl/internal/adapter"
	"rollctl/internal/store"
)

func newSim(t *testing.T, capacity int) (*adapter.Simulator, *store.Store) {
	t.Helper()
	st, err := store.Open(context.Background(), filepath.Join(t.TempDir(), "sim.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	sim, err := adapter.NewSimulator(context.Background(), st, capacity)
	if err != nil {
		t.Fatalf("new simulator: %v", err)
	}
	return sim, st
}

// TestNormalLifecycle proves the participant semantics independently of the
// controller: a new process is not ready immediately, becomes ready, and the
// ready signal stays honest across ticks.
func TestNormalLifecycle(t *testing.T) {
	ctx := context.Background()
	sim, _ := newSim(t, 4)
	if err := sim.SetBehavior(ctx, "w", "v1", adapter.Behavior{Mode: adapter.ModeNormal, ReadyDelayTicks: 2}); err != nil {
		t.Fatal(err)
	}
	sim.SetTick(1)
	id, err := sim.Start(ctx, adapter.StartMeta{Workload: "w", Revision: "v1"})
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	// Tick 1 (age 0): running but not ready.
	s, err := sim.Observe(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	if !s.Running || s.Ready {
		t.Fatalf("age0: running=%v ready=%v, want true/false", s.Running, s.Ready)
	}
	// Tick 2 (age 1): still ramping.
	sim.SetTick(2)
	s, _ = sim.Observe(ctx, id)
	if !s.Running || s.Ready {
		t.Fatalf("age1: running=%v ready=%v, want true/false", s.Running, s.Ready)
	}
	// Tick 3 (age 2): ready.
	sim.SetTick(3)
	s, _ = sim.Observe(ctx, id)
	if !s.Running || !s.Ready {
		t.Fatalf("age2: running=%v ready=%v, want true/true", s.Running, s.Ready)
	}
}

func TestStartRejected(t *testing.T) {
	ctx := context.Background()
	sim, _ := newSim(t, 4)
	if err := sim.SetBehavior(ctx, "w", "bad", adapter.Behavior{Mode: adapter.ModeStartRejected}); err != nil {
		t.Fatal(err)
	}
	if _, err := sim.Start(ctx, adapter.StartMeta{Workload: "w", Revision: "bad"}); err == nil {
		t.Fatal("start should be rejected")
	}
	if sim.LiveCount() != 0 {
		t.Fatalf("rejected start occupied a slot: live=%d", sim.LiveCount())
	}
}

func TestCrashAfterStart(t *testing.T) {
	ctx := context.Background()
	sim, _ := newSim(t, 4)
	if err := sim.SetBehavior(ctx, "w", "c", adapter.Behavior{Mode: adapter.ModeCrashAfterStart, ExitAfterTicks: 2}); err != nil {
		t.Fatal(err)
	}
	sim.SetTick(1)
	id, err := sim.Start(ctx, adapter.StartMeta{Workload: "w", Revision: "c"})
	if err != nil {
		t.Fatal(err)
	}
	sim.SetTick(2)
	if s, _ := sim.Observe(ctx, id); !s.Running {
		t.Fatal("age1 should be running")
	}
	sim.SetTick(3)
	s, err := sim.Observe(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	if s.Running || s.Reason != adapter.ReasonExited {
		t.Fatalf("age2: running=%v reason=%q, want exited", s.Running, s.Reason)
	}
}

func TestFlapAlternates(t *testing.T) {
	ctx := context.Background()
	sim, _ := newSim(t, 4)
	b := adapter.Behavior{Mode: adapter.ModeFlap, ReadyDelayTicks: 1, FlapReadyTicks: 1, FlapDownTicks: 1}
	if err := sim.SetBehavior(ctx, "w", "f", b); err != nil {
		t.Fatal(err)
	}
	sim.SetTick(1)
	id, _ := sim.Start(ctx, adapter.StartMeta{Workload: "w", Revision: "f"})
	want := []bool{false /*age0 ramp*/, true /*age2*/, false, true, false}
	for i, w := range want {
		sim.SetTick(int64(1 + i))
		s, err := sim.Observe(ctx, id)
		if err != nil {
			t.Fatal(err)
		}
		if s.Ready != w {
			t.Fatalf("age %d ready=%v want %v", i, s.Ready, w)
		}
	}
}

func TestCapacityEnforced(t *testing.T) {
	ctx := context.Background()
	sim, _ := newSim(t, 2)
	if err := sim.SetBehavior(ctx, "w", "v", adapter.Behavior{Mode: adapter.ModeNormal}); err != nil {
		t.Fatal(err)
	}
	sim.SetTick(1)
	if _, err := sim.Start(ctx, adapter.StartMeta{Workload: "w", Revision: "v"}); err != nil {
		t.Fatal(err)
	}
	if _, err := sim.Start(ctx, adapter.StartMeta{Workload: "w", Revision: "v"}); err != nil {
		t.Fatal(err)
	}
	if _, err := sim.Start(ctx, adapter.StartMeta{Workload: "w", Revision: "v"}); err == nil {
		t.Fatal("third start must fail with capacity")
	}
	// Terminating one frees a slot.
	ids := sim.ProcessIDsForTest()
	if len(ids) != 2 {
		t.Fatalf("want 2 processes, got %d", len(ids))
	}
	_ = sim.Terminate(ctx, ids[0])
	if _, err := sim.Start(ctx, adapter.StartMeta{Workload: "w", Revision: "v"}); err != nil {
		t.Fatalf("start after terminate: %v", err)
	}
}

// TestReattachAcrossRestart verifies process rows persist so a brand-new
// simulator instance reconstructs the fleet and continues readiness timing.
func TestReattachAcrossRestart(t *testing.T) {
	ctx := context.Background()
	st, err := store.Open(context.Background(), filepath.Join(t.TempDir(), "r.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	sim, err := adapter.NewSimulator(ctx, st, 4)
	if err != nil {
		t.Fatal(err)
	}
	if err := sim.SetBehavior(ctx, "w", "v", adapter.Behavior{Mode: adapter.ModeNormal, ReadyDelayTicks: 1}); err != nil {
		t.Fatal(err)
	}
	sim.SetTick(1)
	id, err := sim.Start(ctx, adapter.StartMeta{Workload: "w", Revision: "v"})
	if err != nil {
		t.Fatal(err)
	}
	// New simulator over the same store (controller restart).
	sim2, err := adapter.NewSimulator(ctx, st, 4)
	if err != nil {
		t.Fatal(err)
	}
	sim2.SetTick(2)
	s, err := sim2.Observe(ctx, id)
	if err != nil {
		t.Fatalf("observe after reattach: %v", err)
	}
	if !s.Running || !s.Ready {
		t.Fatalf("after reattach at tick2: running=%v ready=%v", s.Running, s.Ready)
	}
}
