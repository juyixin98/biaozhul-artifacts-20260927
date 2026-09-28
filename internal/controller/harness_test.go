package controller_test

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"rollctl/internal/adapter"
	"rollctl/internal/controller"
	"rollctl/internal/model"
	"rollctl/internal/store"
)

// harness wires the real store + simulator + controller over a temp SQLite
// file. Nothing here is a mock of the core: the core is the system under test.
type harness struct {
	t   *testing.T
	ctx context.Context
	dir string
	st  *store.Store
	sim *adapter.Simulator
	ctl *controller.Controller
}

func newHarness(t *testing.T, capacity int) *harness {
	t.Helper()
	dir := t.TempDir()
	ctx := context.Background()
	st, err := store.Open(ctx, filepath.Join(dir, "rollctl.db"))
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	sim, err := adapter.NewSimulator(ctx, st, capacity)
	if err != nil {
		t.Fatalf("new simulator: %v", err)
	}
	ctl, err := controller.New(ctx, st, sim, controller.Options{})
	if err != nil {
		t.Fatalf("new controller: %v", err)
	}
	return &harness{t: t, ctx: ctx, dir: dir, st: st, sim: sim, ctl: ctl}
}

// reattach simulates a controller restart: new simulator and controller
// instances reattach to the same SQLite file. The old instances are closed.
func (h *harness) reattach(capacity int) *harness {
	h.t.Helper()
	sim, err := adapter.NewSimulator(h.ctx, h.st, capacity)
	if err != nil {
		h.t.Fatalf("reattach simulator: %v", err)
	}
	ctl, err := controller.New(h.ctx, h.st, sim, controller.Options{})
	if err != nil {
		h.t.Fatalf("reattach controller: %v", err)
	}
	h.sim, h.ctl = sim, ctl
	return h
}

func (h *harness) behavior(workload, revision string, b adapter.Behavior) {
	h.t.Helper()
	if err := h.sim.SetBehavior(h.ctx, workload, revision, b); err != nil {
		h.t.Fatalf("set behavior %s/%s: %v", workload, revision, err)
	}
}

func (h *harness) healthy(revision string) {
	h.behavior("app", revision, adapter.Behavior{Mode: adapter.ModeNormal, ReadyDelayTicks: 1})
}

func (h *harness) tick() controller.TickResult {
	h.t.Helper()
	res, err := h.ctl.Tick(h.ctx)
	if err != nil {
		h.t.Fatalf("tick: %v", err)
	}
	return res
}

func (h *harness) ticks(n int) {
	for i := 0; i < n; i++ {
		h.tick()
	}
}

// driveUntil runs ticks until cond(status) is true or max ticks elapse.
func (h *harness) driveUntil(max int, cond func(*controller.WorkloadStatus) bool) *controller.WorkloadStatus {
	h.t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for i := 0; i < max; i++ {
		if time.Now().After(deadline) {
			h.t.Fatalf("driveUntil timed out")
		}
		h.tick()
		st, err := h.ctl.Status(h.ctx, "app")
		if err != nil {
			h.t.Fatalf("status: %v", err)
		}
		if cond(st) {
			return st
		}
	}
	st, _ := h.ctl.Status(h.ctx, "app")
	h.t.Fatalf("driveUntil: condition not met after %d ticks; status=%+v", max, st)
	return nil
}

func (h *harness) status() *controller.WorkloadStatus {
	h.t.Helper()
	st, err := h.ctl.Status(h.ctx, "app")
	if err != nil {
		h.t.Fatalf("status: %v", err)
	}
	return st
}

func (h *harness) createWorkload(replicas int, rev string, pol model.Policy) {
	h.t.Helper()
	if _, err := h.ctl.CreateWorkload(h.ctx, controller.CreateWorkloadInput{
		Name: "app", Replicas: replicas, Revision: rev, Policy: pol, RequestID: "req-bootstrap",
	}); err != nil {
		h.t.Fatalf("create workload: %v", err)
	}
}

func (h *harness) release(rev string, pol *model.Policy) *model.Release {
	h.t.Helper()
	r, err := h.ctl.CreateRelease(h.ctx, controller.CreateReleaseInput{
		Workload: "app", Revision: rev, Policy: pol, RequestID: "req-" + rev,
	})
	if err != nil {
		h.t.Fatalf("create release %s: %v", rev, err)
	}
	return r
}

func (h *harness) getRelease(id string) *model.Release {
	h.t.Helper()
	r, err := h.ctl.Release(h.ctx, id)
	if err != nil {
		h.t.Fatalf("get release %s: %v", id, err)
	}
	return r
}

func (h *harness) settleBootstrap() {
	h.driveUntil(60, func(st *controller.WorkloadStatus) bool {
		return st.Available == st.Baseline && st.Live == st.Baseline
	})
}

// assertInvariants checks the per-step replica constraints against the
// release policy that is currently in flight (or the last one given).
func (h *harness) assertInvariants(st *controller.WorkloadStatus, pol model.Policy) {
	h.t.Helper()
	minAvail := st.Baseline - pol.MaxUnavailable
	if minAvail < 0 {
		minAvail = 0
	}
	maxLive := st.Baseline + pol.MaxSurge
	if st.Live > maxLive {
		h.t.Errorf("maxSurge violated: live=%d > baseline(%d)+surge(%d)=%d", st.Live, st.Baseline, pol.MaxSurge, maxLive)
	}
	if st.Available < minAvail {
		h.t.Errorf("maxUnavailable violated: available=%d < baseline(%d)-unavailable(%d)=%d", st.Available, st.Baseline, pol.MaxUnavailable, minAvail)
	}
	if st.Live < st.Available {
		h.t.Errorf("impossible: live=%d < available=%d", st.Live, st.Available)
	}
}

// driveReleaseInvarianted ticks and checks invariants after EVERY tick while
// the release is active, stopping when it leaves active state.
func (h *harness) driveReleaseInvarianted(rel *model.Release, pol model.Policy, max int) *model.Release {
	h.t.Helper()
	for i := 0; i < max; i++ {
		h.tick()
		cur := h.getRelease(rel.ID)
		if cur.State == model.RelPending || cur.State == model.RelActive {
			h.assertInvariants(h.status(), pol)
		}
		if cur.State == model.RelSucceeded || cur.State == model.RelFailed {
			return cur
		}
	}
	h.t.Fatalf("release %s never finished after %d ticks", rel.ID, max)
	return nil
}
