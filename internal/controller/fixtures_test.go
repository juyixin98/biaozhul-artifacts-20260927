package controller_test

import (
	"testing"

	"rollctl/internal/adapter"
	"rollctl/internal/controller"
	"rollctl/internal/model"
)

// TestHappyRollout verifies a healthy surge rollout:
//   - maxSurge / maxUnavailable hold at EVERY tick;
//   - created instances are not counted as available until threshold ready
//     observations;
//   - old instances are removed only after new ones are available;
//   - the workload ends fully on the new revision.
func TestHappyRollout(t *testing.T) {
	h := newHarness(t, 16)
	pol := model.Policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 2, DeadlineTicks: 30}
	h.healthy("v1")
	h.healthy("v2")
	h.createWorkload(3, "v1", pol)
	h.settleBootstrap()

	if st := h.status(); st.Available != 3 || st.Workload.CurrentRevision != "v1" {
		t.Fatalf("bootstrap not steady: %+v", st)
	}

	rel := h.release("v2", nil)
	final := h.driveReleaseInvarianted(rel, pol, 60)
	if final.State != model.RelSucceeded {
		t.Fatalf("expected succeeded, got %s: %s", final.State, final.FailMessage)
	}

	st := h.status()
	if st.Workload.CurrentRevision != "v2" {
		t.Fatalf("current revision = %q, want v2", st.Workload.CurrentRevision)
	}
	if st.Available != 3 || st.Live != 3 {
		t.Fatalf("final fleet live=%d available=%d, want 3/3", st.Live, st.Available)
	}
	if rc := st.ByRevision["v2"]; rc.Live != 3 || rc.Available != 3 {
		t.Fatalf("v2 counts = %+v, want live=3 available=3", rc)
	}
	if rc := st.ByRevision["v1"]; rc.Live != 0 || rc.Available != 0 {
		t.Fatalf("old v1 still counts: %+v", rc)
	}
	// No failed/tombstoned-via-failure instances on a healthy rollout.
	for _, ins := range st.Instances {
		if ins.State == model.StateFailed {
			t.Fatalf("unexpected failed instance %s on healthy rollout", ins.ID)
		}
	}
}

// TestCreatedIsNotReady proves the core rule: a freshly started instance is
// StateStarting with ReadyStreak 0 and contributes neither live-available
// capacity... (it IS live, but not available). With threshold=2 it needs two
// later ready observations before promotion.
func TestCreatedIsNotReady(t *testing.T) {
	h := newHarness(t, 16)
	pol := model.Policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 2, DeadlineTicks: 30}
	h.healthy("v1")
	h.healthy("v2")
	h.createWorkload(2, "v1", pol)
	h.settleBootstrap()
	rel := h.release("v2", nil)

	// Tick 1: release activates and exactly one new instance starts.
	h.tick()
	cur := h.getRelease(rel.ID)
	if cur.State != model.RelActive {
		t.Fatalf("release state = %s, want active", cur.State)
	}
	st := h.status()
	newStarting := 0
	for _, ins := range st.Instances {
		if ins.Revision == "v2" {
			if ins.State != model.StateStarting || ins.ReadyStreak != 0 {
				t.Fatalf("new instance state=%s streak=%d, want starting/0", ins.State, ins.ReadyStreak)
			}
			newStarting++
		}
	}
	if newStarting != 1 {
		t.Fatalf("want exactly 1 new starting instance, got %d", newStarting)
	}
	if rc := st.ByRevision["v2"]; rc.Live != 1 || rc.Available != 0 {
		t.Fatalf("created instance must be live but NOT available, got %+v", rc)
	}
	// Surge budget respected at the very first step: 2 old + 1 new = 3.
	if st.Live != 3 {
		t.Fatalf("live = %d, want 3 (baseline 2 + surge 1)", st.Live)
	}
	h.assertInvariants(st, pol)

	// Tick 2: one ready observation -> streak 1, still not available.
	h.tick()
	st = h.status()
	if rc := st.ByRevision["v2"]; rc.Available != 0 {
		t.Fatalf("after one ready observation available=%d, want 0", rc.Available)
	}
	h.assertInvariants(st, pol)

	// Tick 3: second ready observation -> promoted to ready, available now 1.
	h.tick()
	st = h.status()
	if rc := st.ByRevision["v2"]; rc.Available != 1 {
		t.Fatalf("after two ready observations v2 available=%d, want 1", rc.Available)
	}
}

// TestOrderingOldRemovedOnlyAfterNewReady checks the explainable ordering
// directly: in the event sequence, no old instance is terminated before at
// least one new instance is available; and every termination frees the slot
// that the next start consumes (live count never exceeds baseline+surge).
func TestOrderingOldRemovedOnlyAfterNewReady(t *testing.T) {
	h := newHarness(t, 16)
	pol := model.Policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 1, DeadlineTicks: 30}
	h.healthy("v1")
	h.healthy("v2")
	h.createWorkload(3, "v1", pol)
	h.settleBootstrap()

	rel := h.release("v2", nil)
	finished := h.driveReleaseInvarianted(rel, pol, 90)
	if finished.State != model.RelSucceeded {
		t.Fatalf("rollout did not succeed: %s", finished.FailMessage)
	}

	evs, err := h.ctl.Events(h.ctx, "app", 0)
	if err != nil {
		t.Fatalf("events: %v", err)
	}
	newReady := 0
	terminationsBeforeReady := 0
	type ts struct{ ready, term int }
	seq := ts{}
	for _, e := range evs {
		if e.ReleaseID != rel.ID {
			continue
		}
		switch e.Type {
		case model.EvInstanceReady:
			if e.Revision == "v2" {
				newReady++
				seq.ready++
			}
		case model.EvInstanceTerminated:
			seq.term++
			if newReady == 0 {
				terminationsBeforeReady++
			}
		}
	}
	if terminationsBeforeReady != 0 {
		t.Fatalf("%d old instances removed before any new instance became ready", terminationsBeforeReady)
	}
	if seq.ready != 3 || seq.term != 3 {
		t.Fatalf("want 3 new-ready and 3 terminations, got %+v", seq)
	}
}

// TestFixtureStartRejected: the process manager refuses starts (no process is
// ever created). With MaxStartFailures=0 the release fails with the exact
// category start_failed, the old revision keeps serving all replicas, and
// every tick respected the budgets.
func TestFixtureStartRejected(t *testing.T) {
	h := newHarness(t, 16)
	pol := model.Policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 2, DeadlineTicks: 30, MaxStartFailures: 0}
	h.healthy("v1")
	h.behavior("app", "v2", adapter.Behavior{Mode: adapter.ModeStartRejected})
	h.createWorkload(2, "v1", pol)
	h.settleBootstrap()

	before := h.status()
	if before.Available != 2 {
		t.Fatalf("precondition: available=%d, want 2", before.Available)
	}

	rel := h.release("v2", nil)
	final := h.driveReleaseInvarianted(rel, pol, 60)

	if final.State != model.RelFailed {
		t.Fatalf("state = %s, want failed", final.State)
	}
	if final.FailureCategory != model.FailStartFailed {
		t.Fatalf("failure category = %q, want %q", final.FailureCategory, model.FailStartFailed)
	}
	st := h.status()
	if st.Workload.CurrentRevision != "v1" {
		t.Fatalf("current revision changed to %q; failed release must leave v1 serving", st.Workload.CurrentRevision)
	}
	if st.Available != 2 || st.ByRevision["v1"].Available != 2 {
		t.Fatalf("old serving capacity changed: %+v", st.ByRevision)
	}
	// The failed new instance is recorded as failed (auditability) but never
	// occupied a live slot and never looked ready.
	newFailed := 0
	for _, ins := range st.Instances {
		if ins.Revision == "v2" {
			if ins.State != model.StateFailed || ins.FailCategory != model.FailStartFailed || ins.ProcID != "" {
				t.Fatalf("rejected instance not recorded correctly: %+v", ins)
			}
			newFailed++
		}
	}
	if newFailed != 1 {
		t.Fatalf("want exactly 1 failed v2 instance row, got %d", newFailed)
	}
}

// TestFixtureCrashAfterStart: starts succeed but the process exits during
// startup, before reaching readiness. Start-failure budget exhausted ->
// start_failed (exit during startup is a start failure), old revision intact.
func TestFixtureCrashAfterStart(t *testing.T) {
	h := newHarness(t, 16)
	pol := model.Policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 2, DeadlineTicks: 30, MaxStartFailures: 1}
	h.healthy("v1")
	h.behavior("app", "v2", adapter.Behavior{Mode: adapter.ModeCrashAfterStart, ExitAfterTicks: 1})
	h.createWorkload(2, "v1", pol)
	h.settleBootstrap()

	rel := h.release("v2", nil)
	final := h.driveReleaseInvarianted(rel, pol, 90)

	if final.State != model.RelFailed || final.FailureCategory != model.FailStartFailed {
		t.Fatalf("want failed/start_failed, got %s/%q (%s)", final.State, final.FailureCategory, final.FailMessage)
	}
	st := h.status()
	if st.ByRevision["v1"].Available != 2 || st.Workload.CurrentRevision != "v1" {
		t.Fatalf("old revision must keep serving: rev=%s counts=%+v", st.Workload.CurrentRevision, st.ByRevision)
	}
	// Crashed instances must never have been counted available.
	for _, ins := range st.Instances {
		if ins.Revision == "v2" && ins.State != model.StateFailed {
			t.Fatalf("v2 instance %s state=%s, want failed", ins.ID, ins.State)
		}
	}
}

// TestFixtureReadinessFlapping: readiness oscillates and never holds for the
// threshold. No hard start error occurs; the release eventually fails with
// readiness_flapping and the exact reason recorded.
func TestFixtureReadinessFlapping(t *testing.T) {
	h := newHarness(t, 16)
	pol := model.Policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 3, DeadlineTicks: 8, MaxStartFailures: 0}
	h.healthy("v1")
	h.behavior("app", "v2", adapter.Behavior{
		Mode: adapter.ModeFlap, ReadyDelayTicks: 1, FlapReadyTicks: 1, FlapDownTicks: 1,
	})
	h.createWorkload(2, "v1", pol)
	h.settleBootstrap()

	rel := h.release("v2", nil)
	final := h.driveReleaseInvarianted(rel, pol, 120)
	if final.State != model.RelFailed {
		t.Fatalf("state = %s, want failed", final.State)
	}
	if final.FailureCategory != model.FailReadinessFlapping {
		t.Fatalf("category = %q, want %q", final.FailureCategory, model.FailReadinessFlapping)
	}
	st := h.status()
	// Flapping instance must never have been promoted, hence never available.
	if rc := st.ByRevision["v2"]; rc.Available != 0 {
		t.Fatalf("flapping v2 was counted available: %+v", rc)
	}
	if st.ByRevision["v1"].Available != 2 {
		t.Fatalf("old availability = %d, want 2", st.ByRevision["v1"].Available)
	}

	// The event stream must explicitly show the jitter (uncertain conclusion).
	evs, _ := h.ctl.Events(h.ctx, "app", 0)
	sawJitter := false
	for _, e := range evs {
		if e.ReleaseID == rel.ID && (e.Type == model.EvInstanceNotReady) && !e.Certain {
			sawJitter = true
		}
	}
	if !sawJitter {
		t.Fatalf("expected an uncertain readiness-reset event for the flap fixture")
	}
}

// TestFixtureReadyLost: instance reaches ready (threshold satisfied) then
// loses readiness. Two consequences are asserted:
//  1. it immediately stops counting as available (demoted to starting);
//  2. a rollout blocked behind that availability fails as readiness_flapping
//     rather than being reported as a successful cut-over.
func TestFixtureReadyLost(t *testing.T) {
	h := newHarness(t, 16)
	// Threshold 1 so the instance promotes on the first ready tick; holds for
	// 1 tick, then loses readiness permanently. maxUnavailable=1 so the later
	// availability loss is inside the declared budget; the release still must
	// not be reported as a successful cut-over.
	pol := model.Policy{MaxSurge: 2, MaxUnavailable: 1, ReadyThresholdTicks: 1, DeadlineTicks: 6, MaxStartFailures: 0}
	h.healthy("v1")
	h.behavior("app", "v2", adapter.Behavior{
		Mode: adapter.ModeReadyLost, ReadyDelayTicks: 1, ReadyHoldTicks: 1,
	})
	h.createWorkload(2, "v1", pol)
	h.settleBootstrap()

	rel := h.release("v2", nil)
	final := h.driveReleaseInvarianted(rel, pol, 120)
	if final.State != model.RelFailed || final.FailureCategory != model.FailReadinessFlapping {
		t.Fatalf("want failed/readiness_flapping, got %s/%q", final.State, final.FailureCategory)
	}

	// Find the v2 instance: it may have promoted at some point but by the end
	// must be demoted and not available.
	st := h.status()
	for _, ins := range st.Instances {
		if ins.Revision == "v2" && ins.Live() {
			if ins.Available() {
				t.Fatalf("v2 instance %s still counted available after losing readiness", ins.ID)
			}
		}
	}
	evs, _ := h.ctl.Events(h.ctx, "app", 0)
	sawLost := false
	for _, e := range evs {
		if e.ReleaseID == rel.ID && e.Type == model.EvInstanceReadyLost && !e.Certain {
			sawLost = true
		}
	}
	if !sawLost {
		t.Fatalf("expected an uncertain instance_ready_lost event")
	}
}

// TestFixtureInsufficientCapacity: manager capacity equals the baseline and
// the rollout asks for surge=1, so the new start is refused by the manager
// with no room to trade an old slot (maxUnavailable=0). The controller emits
// uncertain capacity-blocked events and eventually fails with
// insufficient_capacity; availability is never broken.
func TestFixtureInsufficientCapacity(t *testing.T) {
	h := newHarness(t, 3)
	pol := model.Policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 2, DeadlineTicks: 5, MaxStartFailures: 0}
	h.healthy("v1")
	h.healthy("v2")
	h.createWorkload(3, "v1", pol)
	h.settleBootstrap()

	rel := h.release("v2", &pol)
	final := h.driveReleaseInvarianted(rel, pol, 60)
	if final.State != model.RelFailed {
		t.Fatalf("state = %s, want failed", final.State)
	}
	if final.FailureCategory != model.FailInsufficientCapacity {
		t.Fatalf("category = %q, want %q", final.FailureCategory, model.FailInsufficientCapacity)
	}
	st := h.status()
	if st.Live != 3 || st.Available != 3 {
		t.Fatalf("budgets must never have been broken: live=%d available=%d, want 3/3", st.Live, st.Available)
	}
	if st.Workload.CurrentRevision != "v1" {
		t.Fatalf("revision must stay v1, got %q", st.Workload.CurrentRevision)
	}
	evs, _ := h.ctl.Events(h.ctx, "app", 0)
	sawBlocked := false
	for _, e := range evs {
		if e.ReleaseID == rel.ID && e.Type == model.EvBlockedCapacity && !e.Certain {
			sawBlocked = true
		}
	}
	if !sawBlocked {
		t.Fatalf("expected an uncertain blocked_capacity event explaining the fixture")
	}
}

// TestFixturePolicyImpossibleZeroZero: surge=0 AND unavailable=0 can never
// move a single replica. This is distinct from manager capacity: the policy
// itself forbids both adding and removing, and the release fails honestly as
// rollout_stalled rather than being mislabeled a capacity failure.
func TestFixturePolicyImpossibleZeroZero(t *testing.T) {
	h := newHarness(t, 16) // manager has plenty of capacity
	pol := model.Policy{MaxSurge: 0, MaxUnavailable: 0, ReadyThresholdTicks: 2, DeadlineTicks: 4, MaxStartFailures: 0}
	h.healthy("v1")
	h.healthy("v2")
	h.createWorkload(2, "v1", pol)
	h.settleBootstrap()

	rel := h.release("v2", &pol)
	final := h.driveReleaseInvarianted(rel, pol, 40)
	if final.State != model.RelFailed {
		t.Fatalf("state = %s, want failed", final.State)
	}
	if final.FailureCategory != model.FailRolloutStalled {
		t.Fatalf("category = %q, want %q (policy impossibility is not a capacity fault)",
			final.FailureCategory, model.FailRolloutStalled)
	}
	if h.status().Workload.CurrentRevision != "v1" {
		t.Fatalf("revision must stay v1, got %q", h.status().Workload.CurrentRevision)
	}
}

// TestCapacityReleasedAllowsSurgeRollout: capacity equals baseline but
// maxUnavailable=1 lets the controller trade an old slot for a new one, so a
// zero-surge rollout still completes while never dropping available below
// baseline-1.
func TestCapacityReleasedAllowsZeroSurgeRollout(t *testing.T) {
	h := newHarness(t, 3)
	pol := model.Policy{MaxSurge: 0, MaxUnavailable: 1, ReadyThresholdTicks: 1, DeadlineTicks: 40, MaxStartFailures: 0}
	h.healthy("v1")
	h.healthy("v2")
	h.createWorkload(3, "v1", pol)
	h.settleBootstrap()

	rel := h.release("v2", &pol)
	final := h.driveReleaseInvarianted(rel, pol, 200)
	if final.State != model.RelSucceeded {
		t.Fatalf("want succeeded, got %s: %s", final.State, final.FailMessage)
	}
	st := h.status()
	if st.ByRevision["v2"].Available != 3 || st.Live != 3 {
		t.Fatalf("final counts wrong: %+v live=%d", st.ByRevision, st.Live)
	}
}

// TestControllerRestartMidRollout: stop the controller (and simulator) mid
// rollout, construct fresh ones on the same SQLite file, and verify the
// rollout resumes and succeeds with budgets intact across the restart
// boundary. Simulated processes are persisted, so readiness is not lost.
func TestControllerRestartMidRollout(t *testing.T) {
	h := newHarness(t, 16)
	pol := model.Policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 2, DeadlineTicks: 40, MaxStartFailures: 0}
	h.healthy("v1")
	h.healthy("v2")
	h.createWorkload(3, "v1", pol)
	h.settleBootstrap()

	rel := h.release("v2", nil)

	// Drive a few ticks with invariants, then restart while active.
	for i := 0; i < 5; i++ {
		h.tick()
		cur := h.getRelease(rel.ID)
		if cur.State == model.RelActive {
			h.assertInvariants(h.status(), pol)
		}
	}
	cur := h.getRelease(rel.ID)
	if cur.State != model.RelActive {
		t.Fatalf("pre-restart state = %s, want active", cur.State)
	}
	preTick := h.ctl.CurrentTick()
	preSt := h.status()
	if preSt.ByRevision["v2"].Live == 0 {
		t.Fatalf("test setup: expected at least one new live instance before restart")
	}

	// Restart: fresh adapter + controller, same DB file. Tick must continue.
	h.reattach(16)
	if got := h.ctl.CurrentTick(); got != preTick {
		t.Fatalf("tick not recovered after restart: got %d want %d", got, preTick)
	}
	st := h.status()
	if st.Live != preSt.Live || st.Available != preSt.Available ||
		st.ByRevision["v2"].Live != preSt.ByRevision["v2"].Live ||
		st.ByRevision["v2"].Available != preSt.ByRevision["v2"].Available {
		t.Fatalf("fleet changed across restart: before live=%d avail=%d v2=%+v; after live=%d avail=%d v2=%+v",
			preSt.Live, preSt.Available, preSt.ByRevision["v2"], st.Live, st.Available, st.ByRevision["v2"])
	}

	final := h.driveReleaseInvarianted(rel, pol, 90)
	if final.State != model.RelSucceeded {
		t.Fatalf("post-restart rollout = %s: %s", final.State, final.FailMessage)
	}
	if h.status().Workload.CurrentRevision != "v2" {
		t.Fatalf("final revision after restart = %q, want v2", h.status().Workload.CurrentRevision)
	}
}

// TestRollbackIsNewReleaseWithHistory: after a failed rollout, rollback to a
// prior revision is itself a new release row, the old rows remain, and a
// later healthy rollout plus second rollback shows full retained history.
func TestRollbackIsNewReleaseWithHistory(t *testing.T) {
	h := newHarness(t, 16)
	pol := model.Policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 2, DeadlineTicks: 30, MaxStartFailures: 0}
	h.healthy("v1")
	h.healthy("v2")
	h.createWorkload(2, "v1", pol)
	h.settleBootstrap()

	// v1 -> v2 succeeds.
	r2 := h.driveReleaseInvarianted(h.release("v2", nil), pol, 90)
	if r2.State != model.RelSucceeded {
		t.Fatalf("v2 rollout: %s", r2.FailMessage)
	}

	// v2 -> v3 fails (start rejected); current revision must stay v2.
	h.behavior("app", "v3", adapter.Behavior{Mode: adapter.ModeStartRejected})
	r3 := h.driveReleaseInvarianted(h.release("v3", nil), pol, 60)
	if r3.State != model.RelFailed || r3.FailureCategory != model.FailStartFailed {
		t.Fatalf("v3 rollout: %s/%q", r3.State, r3.FailureCategory)
	}
	if h.status().Workload.CurrentRevision != "v2" {
		t.Fatalf("current revision after failed v3 = %q, want v2", h.status().Workload.CurrentRevision)
	}

	// Explicit rollback to v1 is a NEW release of kind rollback.
	rb, err := h.ctl.Rollback(h.ctx, controller.RollbackInput{Workload: "app", TargetRevision: "v1", RequestID: "req-rb-v1"})
	if err != nil {
		t.Fatalf("rollback: %v", err)
	}
	if rb.Kind != model.KindRollback || rb.Revision != "v1" || rb.ID == r2.ID || rb.ID == r3.ID {
		t.Fatalf("rollback must be a distinct new release: %+v", rb)
	}
	rbFinal := h.driveReleaseInvarianted(rb, pol, 90)
	if rbFinal.State != model.RelSucceeded || rbFinal.Kind != model.KindRollback {
		t.Fatalf("rollback release result = %s/%s", rbFinal.State, rbFinal.Kind)
	}
	if h.status().Workload.CurrentRevision != "v1" {
		t.Fatalf("after rollback revision = %q, want v1", h.status().Workload.CurrentRevision)
	}

	// History is retained in full, newest first, with distinct ids and states.
	hist, err := h.ctl.Releases(h.ctx, "app")
	if err != nil {
		t.Fatalf("releases: %v", err)
	}
	if len(hist) != 4 {
		t.Fatalf("history length = %d, want 4 (bootstrap, v2 rollout, v3 failed, v1 rollback)", len(hist))
	}
	wantKinds := []model.ReleaseKind{model.KindRollback, model.KindRollout, model.KindRollout, model.KindBootstrap}
	for i, k := range wantKinds {
		if hist[i].Kind != k {
			t.Fatalf("history[%d].kind = %s, want %s", i, hist[i].Kind, k)
		}
	}
	if hist[0].RollbackOf == "" {
		t.Fatalf("rollback row must record which release it undoes")
	}

	// Every release row carries its originating request id for correlation.
	if hist[0].RequestID != "req-rb-v1" {
		t.Fatalf("rollback request id = %q, want req-rb-v1", hist[0].RequestID)
	}
}

// TestRollbackAutoTarget: with no target revision, rollback picks the most
// recent earlier SUCCEEDED revision that differs from current.
func TestRollbackAutoTarget(t *testing.T) {
	h := newHarness(t, 16)
	pol := model.Policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 1, DeadlineTicks: 30}
	h.healthy("v1")
	h.healthy("v2")
	h.healthy("v3")
	h.createWorkload(1, "v1", pol)
	h.settleBootstrap()
	h.driveReleaseInvarianted(h.release("v2", nil), pol, 90)
	h.driveReleaseInvarianted(h.release("v3", nil), pol, 90)

	rb, err := h.ctl.Rollback(h.ctx, controller.RollbackInput{Workload: "app", RequestID: "req-rb-auto"})
	if err != nil {
		t.Fatalf("auto rollback: %v", err)
	}
	if rb.Revision != "v2" {
		t.Fatalf("auto rollback target = %q, want v2", rb.Revision)
	}
}

// TestDefaultPolicyWhenOmitted: a workload created with the Go zero-value
// policy (i.e. "no policy supplied") must not silently become the provably
// blocked surge=0/unavailable=0 policy; a later rollout must succeed with the
// healthy defaults.
func TestDefaultPolicyWhenOmitted(t *testing.T) {
	h := newHarness(t, 16)
	h.healthy("v1")
	h.healthy("v2")
	h.createWorkload(2, "v1", model.Policy{}) // nothing supplied
	h.settleBootstrap()
	rel := h.release("v2", nil)              // and no per-release policy
	final := h.driveReleaseInvarianted(rel, model.Policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 2, DeadlineTicks: 20}, 90)
	if final.State != model.RelSucceeded {
		t.Fatalf("rollout with omitted policy = %s: %s", final.State, final.FailMessage)
	}
	if final.Policy.MaxSurge != 1 || final.Policy.ReadyThresholdTicks != 2 {
		t.Fatalf("rollout did not inherit healthy default policy: %+v", final.Policy)
	}
}

// TestRequestCorrelation: the request id set on a release appears on its
// events, so failures can be traced end to end.
func TestRequestCorrelation(t *testing.T) {
	h := newHarness(t, 16)
	pol := model.Policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 2, DeadlineTicks: 10}
	h.healthy("v1")
	h.behavior("app", "v2", adapter.Behavior{Mode: adapter.ModeStartRejected})
	h.createWorkload(1, "v1", pol)
	h.settleBootstrap()

	rel := h.release("v2", nil)
	final := h.driveReleaseInvarianted(rel, pol, 30)
	if final.State != model.RelFailed {
		t.Fatalf("want failed, got %s", final.State)
	}
	evs, _ := h.ctl.Events(h.ctx, "app", 0)
	correlated := 0
	var failEv *model.Event
	for _, e := range evs {
		if e.ReleaseID == rel.ID {
			if e.RequestID != "req-v2" {
				t.Fatalf("event %s request id = %q", e.Type, e.RequestID)
			}
			correlated++
			if e.Type == model.EvRolloutFailed {
				failEv = e
			}
		}
	}
	if correlated == 0 || failEv == nil {
		t.Fatalf("release events not correlated: correlated=%d failEv=%v", correlated, failEv)
	}
	if failEv.Category != string(model.FailStartFailed) {
		t.Fatalf("failure event category = %q", failEv.Category)
	}
}
