package coordinator_test

import (
	"sync"
	"testing"
	"time"

	"github.com/local/evictioncoordinator/internal/coordinator"
	"github.com/local/evictioncoordinator/internal/domain"
	"github.com/local/evictioncoordinator/internal/testutil"
)

func reqWithEpoch(ns, group, instance string, epoch int64) coordinator.Request {
	return coordinator.Request{
		Namespace: ns, Group: group, InstanceID: instance,
		ClientEpoch: epoch, HasEpoch: true,
	}
}

// memberIDs returns deterministic replica ids a..n.
func memberIDs(n int) []testutil.MemberSpec {
	out := make([]testutil.MemberSpec, n)
	for i := 0; i < n; i++ {
		out[i] = testutil.MemberSpec{ID: string(rune('a' + i)), Ready: true}
	}
	return out
}

// ---------------------------------------------------------------------------
// Scenario 1: concurrent evictions must never reserve the same slot twice.
// maxUnavailable=1 over 3 ready members: EXACTLY one of many concurrent
// requests must be accepted, the rest must be budget_exhausted.
// ---------------------------------------------------------------------------

func TestScenario_ConcurrentEvictionsNoDoubleSpend(t *testing.T) {
	h := testutil.NewHarness(t, 30*time.Second)
	const replicas = 8
	g := h.SetupGroup(t, domain.Group{
		Name: "web", Namespace: "default", Replicas: replicas,
		BudgetMode: domain.MaxUnavailable, BudgetValue: 1,
	}, memberIDs(replicas))

	const clients = 8
	type res struct {
		accepted bool
		cat      domain.FailureCategory
		appr     string
		inst     string
	}
	results := make([]res, clients)
	var wg sync.WaitGroup
	start := make(chan struct{})
	for i := 0; i < clients; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			inst := string(rune('a' + i))
			d := h.MustEvict(t, g.Namespace, g.Name, inst)
			results[i] = res{d.Accepted, d.Category, d.ApprovalID, inst}
		}(i)
	}
	close(start)
	wg.Wait()

	accepted := 0
	acceptedInstances := map[string]int{}
	acceptedApprovalIDs := map[string]int{}
	categories := map[domain.FailureCategory]int{}
	for _, r := range results {
		if r.accepted {
			accepted++
			acceptedInstances[r.inst]++
			acceptedApprovalIDs[r.appr]++
		} else {
			categories[r.cat]++
		}
	}
	if accepted != 1 {
		t.Fatalf("accepted=%d, want exactly 1; categories=%v", accepted, categories)
	}
	for inst, n := range acceptedInstances {
		if n != 1 {
			t.Fatalf("instance %s accepted %d times, want exactly 1", inst, n)
		}
	}
	for appr, n := range acceptedApprovalIDs {
		if n != 1 {
			t.Fatalf("approval %s issued %d times, want exactly 1", appr, n)
		}
	}
	if got := h.LivePendingCount(t, g.Namespace, g.Name); got != 1 {
		t.Fatalf("pending approvals in DB=%d, want 1 (no double reservation)", got)
	}
	if categories[domain.CatBudgetExhausted] != clients-1 {
		t.Fatalf("budget_exhausted=%d want %d; all categories=%v",
			categories[domain.CatBudgetExhausted], clients-1, categories)
	}
}

// ---------------------------------------------------------------------------
// Scenario 2: approved, then stalled. The eviction is approved, then never
// completes and does not expire within the test window. Its budget slot must
// remain charged: a second eviction on a different member is refused.
// ---------------------------------------------------------------------------

func TestScenario_ApprovedThenStalled_KeepsCharging(t *testing.T) {
	h := testutil.NewHarness(t, 10*time.Minute)
	g := h.SetupGroup(t, domain.Group{
		Name: "api", Namespace: "default", Replicas: 3,
		BudgetMode: domain.MaxUnavailable, BudgetValue: 1,
	}, memberIDs(3))

	first := h.MustEvict(t, g.Namespace, g.Name, "a")
	if !first.Accepted {
		t.Fatalf("first eviction not accepted: category=%s reason=%s", first.Category, first.Reason)
	}
	if first.Snapshot.AvailableSlots != 0 {
		t.Fatalf("post-accept available=%d want 0", first.Snapshot.AvailableSlots)
	}
	// Client stalls: never reports result. Advance well short of the TTL.
	h.Now.Advance(30 * time.Second)
	if _, err := h.Coord.SweepExpiries(h.Ctx); err != nil {
		t.Fatalf("sweep: %v", err)
	}

	second := h.MustEvict(t, g.Namespace, g.Name, "b")
	if second.Accepted {
		t.Fatal("second eviction accepted while first approval is still unfinished")
	}
	if second.Category != domain.CatBudgetExhausted {
		t.Fatalf("second category=%s want budget_exhausted", second.Category)
	}
	if got := h.LivePendingCount(t, g.Namespace, g.Name); got != 1 {
		t.Fatalf("pending=%d want 1", got)
	}
}

// ---------------------------------------------------------------------------
// Scenario 2b: after the stalled approval EXPIRES, the slot is STILL charged
// until an explicit, confirmed reclamation. Then a new eviction is accepted.
// ---------------------------------------------------------------------------

func TestScenario_ExpiryThenExplicitReclaim(t *testing.T) {
	h := testutil.NewHarness(t, 10*time.Second)
	g := h.SetupGroup(t, domain.Group{
		Name: "api", Namespace: "default", Replicas: 3,
		BudgetMode: domain.MaxUnavailable, BudgetValue: 1,
	}, memberIDs(3))

	first := h.MustEvict(t, g.Namespace, g.Name, "a")
	if !first.Accepted {
		t.Fatalf("first not accepted: %s", first.Category)
	}

	h.Now.Advance(11 * time.Second)
	sweep, err := h.Coord.SweepExpiries(h.Ctx)
	if err != nil {
		t.Fatalf("sweep: %v", err)
	}
	if len(sweep.Expired) != 1 {
		t.Fatalf("expired=%d want 1", len(sweep.Expired))
	}

	// Slot still charged after expiry: reclaim required.
	blocked := h.MustEvict(t, g.Namespace, g.Name, "b")
	if blocked.Accepted || blocked.Category != domain.CatBudgetExhausted {
		t.Fatalf("after expiry without reclaim: accepted=%v cat=%s", blocked.Accepted, blocked.Category)
	}

	// Reclaim without confirmation must fail and release nothing.
	if _, err := h.Coord.Reclaim(h.Ctx, first.ApprovalID, ""); err == nil {
		t.Fatal("reclaim without confirmation token unexpectedly succeeded")
	}
	stillBlocked := h.MustEvict(t, g.Namespace, g.Name, "b")
	if stillBlocked.Accepted {
		t.Fatal("budget released despite missing confirmation")
	}

	// Confirmed reclaim releases exactly one slot.
	if _, err := h.Coord.Reclaim(h.Ctx, first.ApprovalID, first.ApprovalID); err != nil {
		t.Fatalf("confirmed reclaim: %v", err)
	}
	now := h.MustEvict(t, g.Namespace, g.Name, "b")
	if !now.Accepted {
		t.Fatalf("post-reclaim eviction refused: category=%s reason=%s", now.Category, now.Reason)
	}
	if got := h.LivePendingCount(t, g.Namespace, g.Name); got != 1 {
		t.Fatalf("pending=%d want exactly 1 live approval after reclaim+reapprove", got)
	}
}

// ---------------------------------------------------------------------------
// Scenario 3: instance fails INVOLUNTARILY before the eviction request. The
// failure must be reported as instance_failed — never disguised as a
// budget-blocked rejection — and it must consume real availability.
// ---------------------------------------------------------------------------

func TestScenario_FailThenEvict_IsNotBudgetBlocked(t *testing.T) {
	h := testutil.NewHarness(t, 30*time.Second)
	g := h.SetupGroup(t, domain.Group{
		Name: "db", Namespace: "default", Replicas: 3,
		BudgetMode: domain.MaxUnavailable, BudgetValue: 1,
	}, []testutil.MemberSpec{
		{ID: "a", Ready: true, Failed: true, FailReason: "node-not-ready"},
		{ID: "b", Ready: true},
		{ID: "c", Ready: true},
	})

	// Requesting eviction of the already-failed member: must be
	// instance_failed, NOT budget_exhausted.
	d := h.MustEvict(t, g.Namespace, g.Name, "a")
	if d.Accepted {
		t.Fatal("eviction of a failed member was accepted")
	}
	if d.Category != domain.CatInstanceFailed {
		t.Fatalf("category=%s want instance_failed (involuntary failure must not be disguised as a budget denial)", d.Category)
	}
	if d.Snapshot.FailedReplicas != 1 {
		t.Fatalf("snapshot failed=%d want 1", d.Snapshot.FailedReplicas)
	}

	// And the failure really consumed the single budget slot: a voluntary
	// eviction of a healthy member now sees no room (still the honest reason).
	d2 := h.MustEvict(t, g.Namespace, g.Name, "b")
	if d2.Accepted {
		t.Fatal("eviction approved although the involuntary failure already uses the disruption headroom")
	}
	if d2.Category != domain.CatBudgetExhausted {
		t.Fatalf("healthy member category=%s want budget_exhausted", d2.Category)
	}
	if d2.Snapshot.CurrentUnavailable != 1 || d2.Snapshot.FailedReplicas != 1 {
		t.Fatalf("snapshot unavailable=%d failed=%d want 1/1",
			d2.Snapshot.CurrentUnavailable, d2.Snapshot.FailedReplicas)
	}
}

// ---------------------------------------------------------------------------
// Scenario 4: selector change is versioned. An approval under the old epoch
// becomes stale (slot kept until reclaimed); fresh requests require
// current-epoch observations; a stale client epoch is refused as such.
// ---------------------------------------------------------------------------

func TestScenario_SelectorChangeVersioned(t *testing.T) {
	h := testutil.NewHarness(t, 10*time.Minute)
	g := h.SetupGroup(t, domain.Group{
		Name: "worker", Namespace: "default", Replicas: 3,
		BudgetMode: domain.MaxUnavailable, BudgetValue: 1,
	}, memberIDs(3))

	old := h.MustEvict(t, g.Namespace, g.Name, "a")
	if !old.Accepted {
		t.Fatalf("old-epoch eviction: %s", old.Category)
	}
	oldEpoch := g.SelectorEpoch

	// Selector changes -> epoch bumps and the pending approval is marked stale.
	updated, oldEpochCheck, newEpoch, err := h.Coord.ChangeSelector(h.Ctx, g.Namespace, g.Name, "")
	if err != nil {
		t.Fatalf("change selector: %v", err)
	}
	if oldEpochCheck != oldEpoch {
		t.Fatalf("oldEpoch=%d want %d", oldEpochCheck, oldEpoch)
	}
	if newEpoch != oldEpoch+1 {
		t.Fatalf("newEpoch=%d want %d", newEpoch, oldEpoch+1)
	}

	staleApproval, err := h.Coord.GetApproval(h.Ctx, old.ApprovalID)
	if err != nil {
		t.Fatalf("get approval: %v", err)
	}
	if staleApproval.State != domain.ApprovalStale {
		t.Fatalf("approval state=%s want stale after selector change", staleApproval.State)
	}

	// Until reclaimed, the stale slot stays charged. Fresh-epoch observations
	// now exist for none of the members (old observations are epoch-tagged), so
	// first we expect unknown_readiness — cannot decide, not a flat denial.
	undecidable := h.MustEvict(t, g.Namespace, g.Name, "b")
	if undecidable.Accepted || undecidable.Category != domain.CatUnknownReadiness {
		t.Fatalf("no fresh observation: accepted=%v cat=%s want unknown_readiness",
			undecidable.Accepted, undecidable.Category)
	}

	// A client presenting the stale client epoch gets the explicit category.
	decStaleClient, err := h.Coord.Evict(h.Ctx, reqWithEpoch(g.Namespace, g.Name, "b", oldEpoch))
	if err != nil {
		t.Fatalf("evict stale-client: %v", err)
	}
	if decStaleClient.Category != domain.CatStaleEpoch {
		t.Fatalf("category=%s want stale_selector_epoch", decStaleClient.Category)
	}

	// Provide current-epoch observations for b and c.
	inst := func(id string) domain.Instance {
		return domain.Instance{ID: id, Namespace: updated.Namespace, Group: updated.Name,
			Labels: map[string]string{"app": "web"}}
	}
	if err := h.Adapter.EmitObservation(h.Ctx, inst("b"), true, newEpoch); err != nil {
		t.Fatal(err)
	}
	if err := h.Adapter.EmitObservation(h.Ctx, inst("c"), true, newEpoch); err != nil {
		t.Fatal(err)
	}
	// Member "a" has no current-epoch observation AND holds the stale approval.
	// The group is therefore still not fresh -> cannot decide. The stale slot
	// must be explicitly reclaimed; but reclamation alone does not fabricate an
	// observation, so refresh "a" as well after reclaiming.
	if _, err := h.Coord.Reclaim(h.Ctx, old.ApprovalID, old.ApprovalID); err != nil {
		t.Fatalf("reclaim stale: %v", err)
	}
	if err := h.Adapter.EmitObservation(h.Ctx, inst("a"), true, newEpoch); err != nil {
		t.Fatal(err)
	}

	fresh := h.MustEvict(t, g.Namespace, g.Name, "b")
	if !fresh.Accepted {
		t.Fatalf("fresh-epoch eviction refused: cat=%s reason=%s", fresh.Category, fresh.Reason)
	}
	if fresh.Snapshot.SelectorEpoch != newEpoch {
		t.Fatalf("decision epoch=%d want %d", fresh.Snapshot.SelectorEpoch, newEpoch)
	}
}
