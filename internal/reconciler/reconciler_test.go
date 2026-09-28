package reconciler

import (
	"context"
	"testing"
	"time"

	"infraplanner/internal/model"
	"infraplanner/internal/planner"
	"infraplanner/internal/provider"
)

// 1. Happy path: create an empty-world stack in dependency order.
func TestApply_HappyPath_CreateOrderAndReferences(t *testing.T) {
	f := newFixture(t)

	pr, err := f.rec.Plan(f.ctx, PlanRequest{Resources: desiredStack()})
	if err != nil {
		t.Fatalf("plan: %v", err)
	}
	if len(pr.Operations) != 3 {
		t.Fatalf("plan ops = %d, want 3", len(pr.Operations))
	}
	if pr.Operations[0].Key.Name != "main" ||
		pr.Operations[1].Key.Name != "web" ||
		pr.Operations[2].Key.Name != "api" {
		t.Fatalf("create order = %s,%s,%s; want main,web,api",
			pr.Operations[0].Key.Name, pr.Operations[1].Key.Name, pr.Operations[2].Key.Name)
	}

	ar, err := f.rec.Apply(f.ctx, pr.RunID)
	if err != nil {
		t.Fatalf("apply: %v", err)
	}
	if ar.State != model.RunSucceeded || ar.Completed != 3 {
		t.Fatalf("apply state=%s completed=%d, want succeeded/3 (err=%+v)",
			ar.State, ar.Completed, ar.Error)
	}

	// Every resource exists exactly once with a provider id, and the
	// instance's resolved reference points at the subnet's real id.
	obs, _ := f.sim.Observe(f.ctx)
	byKey := obs.ByKey()
	if len(byKey) != 3 {
		t.Fatalf("live resources = %d, want 3", len(byKey))
	}
	vpcID := byKey[model.Key{Kind: model.KindVPC, Name: "main"}].ID
	sub := byKey[model.Key{Kind: model.KindSubnet, Name: "web"}]
	inst := byKey[model.Key{Kind: model.KindInstance, Name: "api"}]
	if vpcID == "" || sub.ID == "" || inst.ID == "" {
		t.Fatal("every resource must carry a physical id")
	}
	// Resolved references are physical ids, not logical names. We verify the
	// chain through the created map result in the response: subnet's ref was
	// validated by the provider (unresolved would fail the apply).
	if ar.Results[2].State != model.OpSucceeded {
		t.Fatalf("instance op not succeeded: %+v", ar.Results[2])
	}
}

// 2. Destruction order is the reverse of creation order.
func TestApply_DeleteOrderReversed(t *testing.T) {
	f := newFixture(t)
	f.seedStack()

	// keep only the vpc
	pr, err := f.rec.Plan(f.ctx, PlanRequest{Resources: desiredStack()[:1]})
	if err != nil {
		t.Fatal(err)
	}
	if len(pr.Operations) != 2 {
		t.Fatalf("ops = %d, want 2 deletes", len(pr.Operations))
	}
	if pr.Operations[0].Type != planner.OpDelete || pr.Operations[0].Key.Name != "api" {
		t.Fatalf("first = %+v, want delete api", pr.Operations[0])
	}
	if pr.Operations[1].Type != planner.OpDelete || pr.Operations[1].Key.Name != "web" {
		t.Fatalf("second = %+v, want delete web", pr.Operations[1])
	}
	ar, err := f.rec.Apply(f.ctx, pr.RunID)
	if err != nil {
		t.Fatal(err)
	}
	if ar.State != model.RunSucceeded {
		t.Fatalf("state = %s, want succeeded", ar.State)
	}
	obs, _ := f.sim.Observe(f.ctx)
	if len(obs.Resources) != 1 || obs.Resources[0].Key.Name != "main" {
		t.Fatalf("after delete, live = %+v, want only vpc/main", obs.Resources)
	}
}

// 3. Deleting a protected resource is blocked until the guard is released.
func TestApply_ProtectedDeleteNeedsRelease(t *testing.T) {
	f := newFixture(t)
	f.sim.Seed(model.Live{
		Key:       model.Key{Kind: model.KindVPC, Name: "critical"},
		ID:        "live-critical",
		Attrs:     map[string]string{"cidr": "10/8", "region": "east"},
		Protected: true,
	})

	// Plan to delete it (empty desired): must be refused at plan time.
	_, err := f.rec.Plan(f.ctx, PlanRequest{Resources: nil})
	assertErr(t, err, model.CatConflict, "guard_held")

	// The protected resource must still exist.
	obs, _ := f.sim.Observe(f.ctx)
	if len(obs.Resources) != 1 {
		t.Fatalf("blocked plan must not touch the world, count=%d", len(obs.Resources))
	}

	// Re-plan with an explicit release: now applicable.
	pr, err := f.rec.Plan(f.ctx, PlanRequest{
		Resources:     nil,
		ReleaseGuards: []model.Key{{Kind: model.KindVPC, Name: "critical"}},
	})
	if err != nil {
		t.Fatalf("plan after release: %v", err)
	}
	ar, err := f.rec.Apply(f.ctx, pr.RunID)
	if err != nil {
		t.Fatalf("apply: %v", err)
	}
	if ar.State != model.RunSucceeded {
		t.Fatalf("state = %s, want succeeded", ar.State)
	}
	if obs, _ := f.sim.Observe(f.ctx); len(obs.Resources) != 0 {
		t.Fatalf("protected resource not deleted: %+v", obs.Resources)
	}
}

// 4. A mutable change is an in-place update preserving the physical id.
func TestApply_InPlaceUpdatePreservesID(t *testing.T) {
	f := newFixture(t)
	ids := f.seedStack()

	target := desiredStack()
	target[2].Attrs["shape"] = "large" // shape is mutable

	pr, err := f.rec.Plan(f.ctx, PlanRequest{Resources: target})
	if err != nil {
		t.Fatal(err)
	}
	if len(pr.Operations) != 1 || pr.Operations[0].Type != planner.OpUpdate {
		t.Fatalf("ops = %+v, want single update", pr.Operations)
	}
	ar, err := f.rec.Apply(f.ctx, pr.RunID)
	if err != nil {
		t.Fatal(err)
	}
	if ar.Results[0].PhysicalID != ids["api"] {
		t.Fatalf("physical id changed: %s -> %s", ids["api"], ar.Results[0].PhysicalID)
	}
	obs, _ := f.sim.Observe(f.ctx)
	got := obs.ByKey()[model.Key{Kind: model.KindInstance, Name: "api"}]
	if got.ID != ids["api"] || got.Attrs["shape"] != "large" {
		t.Fatalf("update wrong: %+v", got)
	}
}

//  5. An immutable change cascades replacement through dependents, producing
//     new physical ids and destroying the old ones.
func TestApply_ImmutableChangeReplacesChain(t *testing.T) {
	f := newFixture(t)
	ids := f.seedStack()

	target := desiredStack()
	target[0].Attrs["cidr"] = "172.16.0.0/12" // vpc cidr immutable

	pr, err := f.rec.Plan(f.ctx, PlanRequest{Resources: target})
	if err != nil {
		t.Fatal(err)
	}
	// 3 destroys + 3 creates
	if len(pr.Operations) != 6 {
		t.Fatalf("ops = %d, want 6: %+v", len(pr.Operations), pr.Operations)
	}
	ar, err := f.rec.Apply(f.ctx, pr.RunID)
	if err != nil {
		t.Fatal(err)
	}
	if ar.Completed != 6 || ar.State != model.RunSucceeded {
		t.Fatalf("completed=%d state=%s, want 6/succeeded", ar.Completed, ar.State)
	}
	obs, _ := f.sim.Observe(f.ctx)
	if len(obs.Resources) != 3 {
		t.Fatalf("live count = %d, want 3 (no duplicates from replace)", len(obs.Resources))
	}
	for _, l := range obs.Resources {
		if old := ids[l.Key.Name]; l.ID == old {
			t.Fatalf("%s kept old id %s after replace", l.Key, old)
		}
	}
}

// 6. Invalid spec is an input error, never an apply.
func TestPlan_InvalidSpecIsInputError(t *testing.T) {
	f := newFixture(t)
	bad := []model.Desired{{Kind: model.KindVPC, Name: "v"}} // missing cidr/region
	_, err := f.rec.Plan(f.ctx, PlanRequest{Resources: bad})
	assertErr(t, err, model.CatInput, "invalid_spec")
}

// 7. Unknown run and re-apply are state conflicts.
func TestApply_StateConflicts(t *testing.T) {
	f := newFixture(t)
	_, err := f.rec.Apply(f.ctx, "run-does-not-exist")
	assertErr(t, err, model.CatInput, "unknown_run")

	pr, err := f.rec.Plan(f.ctx, PlanRequest{Resources: desiredStack()})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.rec.Apply(f.ctx, pr.RunID); err != nil {
		t.Fatal(err)
	}
	// re-applying a succeeded run is refused (possible drift)
	ar, err := f.rec.Apply(f.ctx, pr.RunID)
	if err != nil {
		t.Fatalf("succeeded re-apply returns response, got err %v", err)
	}
	assertRespErr(t, ar, model.CatConflict, "already_succeeded")
}

// 8. Drift between plan and apply is rejected; the drifted world is untouched.
func TestApply_DriftBeforeApplyRejected(t *testing.T) {
	f := newFixture(t)
	f.seedStack()

	// Plan an in-place update.
	target := desiredStack()
	target[2].Attrs["shape"] = "large"
	pr, err := f.rec.Plan(f.ctx, PlanRequest{Resources: target})
	if err != nil {
		t.Fatal(err)
	}

	// External actor mutates the vpc region between plan and apply.
	f.sim.Seed(model.Live{
		Key: model.Key{Kind: model.KindVPC, Name: "main"}, ID: "live-vpc",
		Attrs: map[string]string{"cidr": "10.0.0.0/8", "region": "WEST-CHANGED"}})
	// Seed with same id: simulate external in-place change by overwriting map.
	f.overwriteLive(model.Live{
		Key: model.Key{Kind: model.KindVPC, Name: "main"}, ID: "live-vpc",
		Attrs: map[string]string{"cidr": "10.0.0.0/8", "region": "WEST-CHANGED"}})

	ar, err := f.rec.Apply(f.ctx, pr.RunID)
	if err != nil {
		t.Fatalf("drift should be a response error, got transport err %v", err)
	}
	assertRespErr(t, ar, model.CatConflict, "drift")

	// The drifted change must remain exactly as the external actor left it.
	obs, _ := f.sim.Observe(f.ctx)
	vpc := obs.ByKey()[model.Key{Kind: model.KindVPC, Name: "main"}]
	if vpc.Attrs["region"] != "WEST-CHANGED" {
		t.Fatalf("drifted resource was mutated by apply: %+v", vpc)
	}
	inst := obs.ByKey()[model.Key{Kind: model.KindInstance, Name: "api"}]
	if inst.Attrs["shape"] != "small" {
		t.Fatalf("update must not run after drift; shape=%s", inst.Attrs["shape"])
	}
}

//  9. Create commits but the success response is lost. The reconciler must
//     settle from the real outcome (observation) and never provision twice —
//     whether it adopts within the same apply loop or after a resume.
func TestApply_CreateSuccessResponseLost_NoDuplicate(t *testing.T) {
	f := newFixture(t)

	pr, err := f.rec.Plan(f.ctx, PlanRequest{Resources: []model.Desired{
		{Kind: model.KindVPC, Name: "main",
			Attrs: map[string]string{"cidr": "10/8", "region": "east"}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	f.sim.ArmFault(provider.Fault{
		Kind:   provider.FaultCreateCommitLost,
		Target: model.Key{Kind: model.KindVPC, Name: "main"},
	})

	ar, err := f.rec.Apply(f.ctx, pr.RunID)
	if err != nil {
		t.Fatal(err)
	}
	// The ambiguous response is settled by re-observation: exactly one
	// committed resource is adopted as success, not a second create.
	if ar.State != model.RunSucceeded || ar.Completed != 1 {
		t.Fatalf("apply state=%s completed=%d err=%+v", ar.State, ar.Completed, ar.Error)
	}
	obs, _ := f.sim.Observe(f.ctx)
	if len(obs.Resources) != 1 {
		t.Fatalf("live count = %d, want exactly 1 (no duplicate create)", len(obs.Resources))
	}
	if ar.Results[0].PhysicalID != obs.Resources[0].ID {
		t.Fatalf("adopted id %s != live id %s", ar.Results[0].PhysicalID, obs.Resources[0].ID)
	}
	// Evidence must show the adopt decision, so the recovery is replayable.
	var sawAdopt bool
	for _, e := range ar.Evidence {
		if e.Kind == "decision" && containsBody(e.Body, "adopted_committed_after_ambiguous_response") {
			sawAdopt = true
		}
	}
	if !sawAdopt {
		t.Fatal("missing adopt decision evidence")
	}
}

// 9b. Cross-process variant: the process dies with the op recorded inflight
//
//	(no chance to re-observe). A fresh resume adopts the committed resource
//	from the real world instead of recreating it.
func TestResume_CommittedInflightAdoptedNotRecreated(t *testing.T) {
	f := newFixture(t)
	key := model.Key{Kind: model.KindVPC, Name: "main"}
	desired := []model.Desired{{Kind: model.KindVPC, Name: "main",
		Attrs: map[string]string{"cidr": "10/8", "region": "east"}}}

	pr, err := f.rec.Plan(f.ctx, PlanRequest{Resources: desired})
	if err != nil {
		t.Fatal(err)
	}
	// Simulate "committed but crashed before journaling success" by seeding
	// the resource as a different process would observe it, and marking the
	// op inflight in the journal.
	f.sim.Seed(model.Live{Key: key, ID: "orphan-committed-id",
		Attrs: map[string]string{"cidr": "10/8", "region": "east"}})
	rows, _ := f.store.ListOps(f.ctx, pr.RunID)
	if len(rows) != 1 {
		t.Fatalf("seeded ops = %d, want 1", len(rows))
	}
	rows[0].State = model.OpInflight
	rows[0].Attempts = 1
	if err := f.store.UpsertOp(f.ctx, pr.RunID, *rows[0]); err != nil {
		t.Fatal(err)
	}
	if err := f.store.SetRunState(f.ctx, pr.RunID, model.RunApplying, nil); err != nil {
		t.Fatal(err)
	}

	ar, err := f.rec.Resume(f.ctx, pr.RunID)
	if err != nil {
		t.Fatalf("resume: %v", err)
	}
	if ar.State != model.RunSucceeded || ar.Completed != 1 {
		t.Fatalf("resume state=%s completed=%d err=%+v", ar.State, ar.Completed, ar.Error)
	}
	if ar.Results[0].PhysicalID != "orphan-committed-id" {
		t.Fatalf("resume adopted %s, want orphan-committed-id", ar.Results[0].PhysicalID)
	}
	obs, _ := f.sim.Observe(f.ctx)
	if len(obs.Resources) != 1 || obs.Resources[0].ID != "orphan-committed-id" {
		t.Fatalf("resume recreated resource: %+v", obs.Resources)
	}
}

// 9c. An ambiguous outcome that is confirmed NOT committed (create_unknown)
//
//	is safely retried within the same run and ultimately succeeds once,
//	with exactly one resource.
func TestApply_OutcomeUnknownRolledBackRetriedInRun(t *testing.T) {
	f := newFixture(t)
	key := model.Key{Kind: model.KindVPC, Name: "main"}
	pr, err := f.rec.Plan(f.ctx, PlanRequest{Resources: []model.Desired{
		{Kind: model.KindVPC, Name: "main",
			Attrs: map[string]string{"cidr": "10/8", "region": "east"}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	f.sim.ArmFault(provider.Fault{Kind: provider.FaultCreateUnknown, Target: key, Remaining: 2})

	ar, err := f.rec.Apply(f.ctx, pr.RunID)
	if err != nil {
		t.Fatal(err)
	}
	if ar.State != model.RunSucceeded {
		t.Fatalf("state = %s, want succeeded: %+v", ar.State, ar.Error)
	}
	// Two ambiguous/rolled-back attempts then one real create.
	if ar.Results[0].Attempts != 3 {
		t.Fatalf("attempts = %d, want 3", ar.Results[0].Attempts)
	}
	obs, _ := f.sim.Observe(f.ctx)
	if len(obs.Resources) != 1 {
		t.Fatalf("live count = %d, want exactly 1 (no duplicate)", len(obs.Resources))
	}
}

func containsBody(body, sub string) bool {
	return len(body) >= len(sub) && indexIn(body, sub) >= 0
}

func indexIn(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

//  10. Process killed mid-create (hang + cancellation); resume re-observes and
//     completes without a duplicate.
func TestResume_InterruptedInflightCreate(t *testing.T) {
	f := newFixture(t)

	pr, err := f.rec.Plan(f.ctx, PlanRequest{Resources: []model.Desired{
		{Kind: model.KindVPC, Name: "main",
			Attrs: map[string]string{"cidr": "10/8", "region": "east"}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	f.sim.ArmFault(provider.Fault{
		Kind:   provider.FaultCreateHang,
		Target: model.Key{Kind: model.KindVPC, Name: "main"},
	})

	cctx, cancel := context.WithCancel(f.ctx)
	go func() {
		// Cancel after the create call is known to be in flight (the hang
		// fault parks the call, so a small delay lands mid-Provider-call).
		time.Sleep(50 * time.Millisecond)
		cancel()
	}()
	ar, err := f.rec.Apply(cctx, pr.RunID)
	if err != nil {
		t.Fatalf("interrupted apply returns response, got %v", err)
	}
	if ar.State != model.RunInterrupted {
		t.Fatalf("state = %s, want interrupted", ar.State)
	}

	// After interruption nothing should have committed for a pure hang.
	obs, _ := f.sim.Observe(f.ctx)
	if len(obs.Resources) != 0 {
		t.Fatalf("hang create should not commit, found %+v", obs.Resources)
	}

	// New process resumes (hang fault already consumed -> real create runs).
	ar2, err := f.rec.Resume(f.ctx, pr.RunID)
	if err != nil {
		t.Fatalf("resume: %v", err)
	}
	if ar2.State != model.RunSucceeded || ar2.Completed != 1 {
		t.Fatalf("resume state=%s completed=%d err=%+v", ar2.State, ar2.Completed, ar2.Error)
	}
	if obs2, _ := f.sim.Observe(f.ctx); len(obs2.Resources) != 1 {
		t.Fatalf("after resume live count = %d, want 1", len(obs2.Resources))
	}
}

// 11. Resume refuses when unrelated external drift occurred after the crash.
func TestResume_ExternalDriftRejected(t *testing.T) {
	f := newFixture(t)
	ids := f.seedStack()
	_ = ids

	// Plan to update instance shape.
	target := desiredStack()
	target[2].Attrs["shape"] = "large"
	pr, err := f.rec.Plan(f.ctx, PlanRequest{Resources: target})
	if err != nil {
		t.Fatal(err)
	}
	// Interrupt during the update.
	f.sim.ArmFault(provider.Fault{
		Kind:   provider.FaultUpdateHang,
		Target: model.Key{Kind: model.KindInstance, Name: "api"},
	})
	cctx, cancel := context.WithCancel(f.ctx)
	go func() {
		time.Sleep(50 * time.Millisecond)
		cancel()
	}()
	_, _ = f.rec.Apply(cctx, pr.RunID)

	// External actor changes the vpc (a resource not operated on) post-crash.
	f.overwriteLive(model.Live{
		Key: model.Key{Kind: model.KindVPC, Name: "main"}, ID: "live-vpc",
		Attrs: map[string]string{"cidr": "10.0.0.0/8", "region": "EXTERNAL"}})

	ar, err := f.rec.Resume(f.ctx, pr.RunID)
	if err != nil {
		t.Fatalf("resume returns response, got %v", err)
	}
	assertRespErr(t, ar, model.CatConflict, "drift")
}

//  12. Resource exhaustion is a distinct failure category and is distinguished
//     from transient compute failures. We arm a persistent injected-exhaustion
//     (fires every attempt) so the op fails the bounded number of retries and
//     the run is marked failed with category resource_exhaustion.
func TestApply_ExhaustionIsDistinctCategory(t *testing.T) {
	f := newFixture(t)

	pr, err := f.rec.Plan(f.ctx, PlanRequest{Resources: []model.Desired{
		{Kind: model.KindVPC, Name: "main",
			Attrs: map[string]string{"cidr": "10/8", "region": "east"}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	f.sim.ArmFault(provider.Fault{
		Kind:      provider.FaultCreateExhaust,
		Target:    model.Key{Kind: model.KindVPC, Name: "main"},
		Remaining: 10, // exceeds MaxAttempts; persists across every retry
	})

	ar, err := f.rec.Apply(f.ctx, pr.RunID)
	if err != nil {
		t.Fatal(err)
	}
	if ar.State != model.RunFailed {
		t.Fatalf("state = %s, want failed", ar.State)
	}
	assertRespErr(t, ar, model.CatExhaustion, "capacity_full")
	if ar.Results[0].Attempts != 3 {
		t.Fatalf("attempts = %d, want 3", ar.Results[0].Attempts)
	}
	// Nothing must have been provisioned.
	if obs, _ := f.sim.Observe(f.ctx); len(obs.Resources) != 0 {
		t.Fatalf("exhausted create left state: %+v", obs.Resources)
	}
}

// 12b. A one-shot exhaustion is retried within the run and then succeeds,
//
//	proving exhaustion is retried but a persistent one fails the run.
func TestApply_OneShotExhaustionRecovers(t *testing.T) {
	f := newFixture(t)
	pr, err := f.rec.Plan(f.ctx, PlanRequest{Resources: []model.Desired{
		{Kind: model.KindVPC, Name: "main",
			Attrs: map[string]string{"cidr": "10/8", "region": "east"}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	f.sim.ArmFault(provider.Fault{
		Kind: provider.FaultCreateExhaust, Remaining: 1,
		Target: model.Key{Kind: model.KindVPC, Name: "main"}})

	ar, err := f.rec.Apply(f.ctx, pr.RunID)
	if err != nil {
		t.Fatal(err)
	}
	if ar.State != model.RunSucceeded {
		t.Fatalf("state = %s, want succeeded: %+v", ar.State, ar.Error)
	}
	if ar.Results[0].Attempts != 2 {
		t.Fatalf("attempts = %d, want 2 (one exhausted + one success)", ar.Results[0].Attempts)
	}
}

//  13. Transient compute failures are retried and then succeed; attempts are
//     recorded as evidence.
func TestApply_TransientRetriedWithEvidence(t *testing.T) {
	f := newFixture(t)
	f.seedStack()
	target := desiredStack()
	target[2].Attrs["shape"] = "large"
	pr, err := f.rec.Plan(f.ctx, PlanRequest{Resources: target})
	if err != nil {
		t.Fatal(err)
	}
	// Fail the update twice, succeed on the third.
	f.sim.ArmFault(provider.Fault{
		Kind: provider.FaultUpdateTransient, Remaining: 2,
		Target: model.Key{Kind: model.KindInstance, Name: "api"}})

	ar, err := f.rec.Apply(f.ctx, pr.RunID)
	if err != nil {
		t.Fatal(err)
	}
	if ar.State != model.RunSucceeded {
		t.Fatalf("state = %s, want succeeded: %+v", ar.State, ar.Error)
	}
	if ar.Results[0].Attempts < 3 {
		t.Fatalf("attempts = %d, want >= 3", ar.Results[0].Attempts)
	}
	ev, err := f.rec.Evidence(f.ctx, pr.RunID)
	if err != nil {
		t.Fatal(err)
	}
	var sawError, sawRequest, sawObserve, sawResponse bool
	for _, e := range ev {
		switch e.Kind {
		case "error":
			sawError = true
		case "request":
			sawRequest = true
		case "observe":
			sawObserve = true
		case "response":
			sawResponse = true
		}
	}
	if !sawError || !sawRequest || !sawObserve || !sawResponse {
		t.Fatalf("evidence incomplete: error=%v request=%v observe=%v response=%v",
			sawError, sawRequest, sawObserve, sawResponse)
	}
}

//  14. An update target vanishes during the run (the provider call was in
//     flight when an external actor deleted it). Resume must surface a state
//     conflict ("vanished"), not silently recreate it or delete elsewhere.
func TestApply_TargetVanishedMidApply(t *testing.T) {
	f := newFixture(t)
	f.seedStack()
	target := desiredStack()
	target[2].Attrs["shape"] = "large"
	pr, err := f.rec.Plan(f.ctx, PlanRequest{Resources: target})
	if err != nil {
		t.Fatal(err)
	}

	// Drive the update op to inflight (interrupted while hung), then have an
	// external actor delete the instance before resume.
	f.sim.ArmFault(provider.Fault{
		Kind:   provider.FaultUpdateHang,
		Target: model.Key{Kind: model.KindInstance, Name: "api"},
	})
	cctx, cancel := context.WithCancel(f.ctx)
	go func() {
		time.Sleep(50 * time.Millisecond)
		cancel()
	}()
	ar0, err := f.rec.Apply(cctx, pr.RunID)
	if err != nil {
		t.Fatal(err)
	}
	if ar0.State != model.RunInterrupted {
		t.Fatalf("setup state = %s, want interrupted", ar0.State)
	}
	f.removeLive(model.Key{Kind: model.KindInstance, Name: "api"})

	ar, err := f.rec.Resume(f.ctx, pr.RunID)
	if err != nil {
		t.Fatal(err)
	}
	assertRespErr(t, ar, model.CatConflict, "vanished")

	// The vpc/subnet must be untouched; the instance must stay deleted.
	obs, _ := f.sim.Observe(f.ctx)
	byKey := obs.ByKey()
	if _, present := byKey[model.Key{Kind: model.KindInstance, Name: "api"}]; present {
		t.Fatal("vanished target must not be silently recreated")
	}
	if byKey[model.Key{Kind: model.KindVPC, Name: "main"}].Attrs["region"] != "east" {
		t.Fatal("unrelated resource mutated")
	}
}
