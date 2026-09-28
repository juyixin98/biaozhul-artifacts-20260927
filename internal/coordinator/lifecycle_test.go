package coordinator_test

import (
	"testing"
	"time"

	"github.com/local/evictioncoordinator/internal/coordinator"
	"github.com/local/evictioncoordinator/internal/domain"
	"github.com/local/evictioncoordinator/internal/testutil"
)

// Replaying the SAME request id must return the original decision and must not
// reserve a second budget slot (honest idempotency).
func TestScenario_IdempotentRequestIDReplay(t *testing.T) {
	h := testutil.NewHarness(t, 30*time.Second)
	g := h.SetupGroup(t, domain.Group{
		Name: "web", Namespace: "default", Replicas: 3,
		BudgetMode: domain.MaxUnavailable, BudgetValue: 1,
	}, memberIDs(3))

	req := coordinator.Request{
		Namespace: "default", Group: "web", InstanceID: "a", RequestID: "fixed-req-1",
	}
	first, err := h.Coord.Evict(h.Ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	if !first.Accepted {
		t.Fatalf("first: %s", first.Category)
	}

	replay, err := h.Coord.Evict(h.Ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	if !replay.Accepted || replay.ApprovalID != first.ApprovalID {
		t.Fatalf("replay must return the same approval: first=%s replay=%s",
			first.ApprovalID, replay.ApprovalID)
	}
	if replay.RequestID != "fixed-req-1" {
		t.Fatalf("request id=%q", replay.RequestID)
	}
	if got := h.LivePendingCount(t, g.Namespace, g.Name); got != 1 {
		t.Fatalf("pending=%d want 1 after replay", got)
	}
}

// minAvailable budget: 4 replicas, minAvailable=3 -> only 1 interruption.
func TestScenario_MinAvailableEnforced(t *testing.T) {
	h := testutil.NewHarness(t, 30*time.Second)
	g := h.SetupGroup(t, domain.Group{
		Name: "web", Namespace: "default", Replicas: 4,
		BudgetMode: domain.MinAvailable, BudgetValue: 3,
	}, memberIDs(4))

	first := h.MustEvict(t, g.Namespace, g.Name, "a")
	if !first.Accepted {
		t.Fatalf("first: cat=%s", first.Category)
	}
	if first.Snapshot.AllowedUnavailable != 1 {
		t.Fatalf("allowed=%d want 1", first.Snapshot.AllowedUnavailable)
	}
	second := h.MustEvict(t, g.Namespace, g.Name, "b")
	if second.Accepted || second.Category != domain.CatBudgetExhausted {
		t.Fatalf("second accepted=%v cat=%s", second.Accepted, second.Category)
	}
}

// Reporting a successful eviction frees the slot, allowing the next eviction.
func TestScenario_ReportSuccessFreesSlot(t *testing.T) {
	h := testutil.NewHarness(t, 30*time.Second)
	g := h.SetupGroup(t, domain.Group{
		Name: "web", Namespace: "default", Replicas: 3,
		BudgetMode: domain.MaxUnavailable, BudgetValue: 1,
	}, memberIDs(3))

	first := h.MustEvict(t, g.Namespace, g.Name, "a")
	if !first.Accepted {
		t.Fatal(first.Category)
	}
	// Member a now turns not-ready as the eviction actuates, and result reported.
	if err := h.Adapter.EmitObservation(h.Ctx,
		domain.Instance{ID: "a", Namespace: g.Namespace, Group: g.Name, Labels: map[string]string{"app": "web"}},
		false, g.SelectorEpoch); err != nil {
		t.Fatal(err)
	}
	if _, err := h.Coord.ReportResult(h.Ctx, first.ApprovalID, true, "evicted"); err != nil {
		t.Fatalf("report: %v", err)
	}
	// a remains unavailable (restarting), so budget still full -> blocked.
	blocked := h.MustEvict(t, g.Namespace, g.Name, "b")
	if blocked.Accepted {
		t.Fatal("slot arithmetic: a still not ready, b must be blocked")
	}
	// a becomes ready again; now the slot is genuinely free.
	if err := h.Adapter.EmitObservation(h.Ctx,
		domain.Instance{ID: "a", Namespace: g.Namespace, Group: g.Name, Labels: map[string]string{"app": "web"}},
		true, g.SelectorEpoch); err != nil {
		t.Fatal(err)
	}
	next := h.MustEvict(t, g.Namespace, g.Name, "b")
	if !next.Accepted {
		t.Fatalf("slot should be free after completion+recovery: cat=%s reason=%s",
			next.Category, next.Reason)
	}
}
